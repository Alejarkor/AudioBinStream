"""
main.py — Servicio de captura y streaming de audio (Nexor / Jetson Orin)

Arranca el pipeline GStreamer, conecta el adaptador MQTT y gestiona el
ciclo de vida completo del servicio. Diseñado para correr como servicio
systemd (Type=simple).

Uso:
    python3 -m audio_capture_service.main
    python3 -m audio_capture_service.main --config /etc/nexor/audio_capture.json
    python3 -m audio_capture_service.main --simulate   # sin hardware real
"""

import argparse
import logging
import os
import signal
import sys
import time
import threading
from typing import Optional

from .config import AudioCaptureConfig
from .device_discovery import find_alsa_device_by_name, list_alsa_capture_devices
from .pipeline import AudioPipeline, PipelineState
from .mqtt_adapter import AudioCaptureServiceAdapter
from .node_runtime import NexorNodeRuntimeConfig
from .rode_controller import RodeController, RodeControllerError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("audio_capture_service")


class AudioCaptureService:
    def __init__(self, cfg: AudioCaptureConfig, simulate: bool = False) -> None:
        self._cfg = cfg
        self._simulate = simulate
        self._start_time: Optional[float] = None
        self._last_error: Optional[str] = None
        self._shutdown_event = threading.Event()
        self._pipeline_restart_count = 0
        self._max_pipeline_restarts = 5
        self._config_path: Optional[str] = None

        self._pipeline = AudioPipeline(
            on_state_change=self._on_pipeline_state_change,
            on_error=self._on_pipeline_error,
        )
        self._mqtt = AudioCaptureServiceAdapter(
            cfg=cfg,
            on_start=self._handle_start,
            on_stop=self._handle_stop,
            on_restart=self._handle_restart,
            on_mute=self._handle_mute,
            on_apply_config=self._handle_apply_config,
            get_state_cb=self._get_state_dict,
        )

        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

    def set_config_path(self, path: Optional[str]) -> None:
        self._config_path = path

    def run(self) -> int:
        logger.info("=" * 60)
        logger.info(f"  Audio Capture Service — node_id={self._cfg.node_id}")
        if self._cfg.is_push_transport:
            logger.info(f"  Destino UDP: {self._cfg.dest_ip}:{self._cfg.dest_port} ({self._cfg.protocol})")
        else:
            logger.info(f"  Endpoint TCP: {self._cfg.stream_bind_ip}:{self._cfg.stream_port} ({self._cfg.protocol})")
        logger.info(f"  Audio: {self._cfg.sample_rate}Hz {self._cfg.channels}ch {self._cfg.bit_depth}bit")
        if self._simulate:
            logger.info("  MODO SIMULACIÓN — sin hardware real")
        logger.info("=" * 60)

        errors = self._cfg.validate()
        if errors:
            for e in errors:
                logger.error(f"Config inválida: {e}")
            return 1

        if not self._mqtt.start():
            logger.warning("No se pudo conectar a MQTT — continuando sin control remoto")

        self._mqtt.publish_state("STARTING", healthy=False)
        self._mqtt.publish_event("service_starting", details={"simulate": self._simulate})

        rode_ok = False
        rode_mode = None
        if not self._simulate:
            rode_ok, rode_mode = self._init_rode()
        else:
            logger.info("[SIMULATE] Saltando inicialización RODE")

        self._mqtt.publish_capabilities(rode_available=rode_ok, rode_mode=rode_mode)

        if not self._simulate:
            alsa_id = self._resolve_alsa_device()
            if alsa_id is None:
                logger.error("No se encontró el dispositivo de audio. Abortando.")
                self._mqtt.publish_state("ERROR", healthy=False, last_error="ALSA device not found")
                self._mqtt.publish_event("service_start_failed", severity="error", details={"reason": "alsa_device_not_found"})
                self._mqtt.stop()
                return 1
            self._cfg.alsa_device_override = alsa_id
        else:
            logger.info("[SIMULATE] Saltando resolución de dispositivo ALSA")

        if not self._simulate:
            ok = self._pipeline.start(self._cfg)
            if not ok:
                logger.error("Pipeline GStreamer no pudo arrancar")
                self._mqtt.publish_state("ERROR", healthy=False, last_error="Pipeline start failed")
                self._mqtt.publish_event("service_start_failed", severity="error", details={"reason": "pipeline_start_failed"})
                self._mqtt.stop()
                return 1
        else:
            logger.info("[SIMULATE] Pipeline no arrancado (modo simulación)")

        self._start_time = time.monotonic()
        self._mqtt.publish_state("RUNNING", healthy=True, pid=os.getpid())
        self._mqtt.publish_config_reported(self._cfg)
        self._mqtt.publish_endpoint(self._cfg)
        self._mqtt.publish_stream_target(self._cfg, source="startup")
        self._mqtt.publish_event("service_started", details={"trigger": "startup", "pid": os.getpid()})
        logger.info("Servicio corriendo. Esperando shutdown...")

        try:
            while not self._shutdown_event.is_set():
                self._shutdown_event.wait(timeout=5.0)
                if self._mqtt.is_connected():
                    self._mqtt.publish_state(
                        status=self._pipeline.state if not self._simulate else "RUNNING",
                        healthy=True,
                        pid=os.getpid(),
                        uptime_s=self._uptime_seconds(),
                    )

                if not self._simulate and not self._shutdown_event.is_set():
                    if self._pipeline.state == PipelineState.ERROR:
                        if self._pipeline_restart_count < self._max_pipeline_restarts:
                            self._pipeline_restart_count += 1
                            logger.warning(f"Reintentando pipeline (intento {self._pipeline_restart_count})")
                            time.sleep(3.0)
                            self._pipeline.restart(self._cfg)
                        else:
                            logger.error("Máximo de reinicios del pipeline alcanzado. Abortando.")
                            break
        except Exception as e:
            logger.error(f"Error inesperado en bucle principal: {e}")

        return self._shutdown()

    def _shutdown(self) -> int:
        logger.info("Iniciando shutdown gracioso...")
        self._mqtt.publish_state("STOPPING", healthy=False)
        self._mqtt.publish_event("service_stopping")
        if not self._simulate:
            self._pipeline.stop()
        self._mqtt.stop()
        logger.info("Servicio detenido.")
        return 0

    def _signal_handler(self, signum, frame) -> None:
        logger.info(f"Señal recibida: {signal.Signals(signum).name}")
        self._shutdown_event.set()

    def _init_rode(self):
        try:
            with RodeController() as rode:
                if not rode.is_available():
                    logger.warning("RØDE AI-Micro no disponible")
                    return False, None
                current_mode = rode.get_mode()
                logger.info(f"RØDE disponible — modo actual: {current_mode}")
                if self._cfg.rode_auto_set_mode and current_mode != self._cfg.rode_mode:
                    logger.info(f"Cambiando modo RØDE: {current_mode} → {self._cfg.rode_mode}")
                    rode.set_mode(self._cfg.rode_mode)
                    current_mode = rode.get_mode()
                return True, current_mode
        except RodeControllerError as e:
            logger.warning(f"No se pudo inicializar RØDE: {e}")
            return False, None
        except Exception as e:
            logger.warning(f"Error inesperado inicializando RØDE: {e}")
            return False, None

    def _resolve_alsa_device(self) -> Optional[str]:
        if self._cfg.alsa_device_override:
            logger.info(f"Usando dispositivo ALSA override: {self._cfg.alsa_device_override}")
            return self._cfg.alsa_device_override

        alsa_id = find_alsa_device_by_name(self._cfg.device_name)
        if alsa_id:
            logger.info(f"Dispositivo ALSA resuelto: '{self._cfg.device_name}' → {alsa_id}")
            return alsa_id

        devices = list_alsa_capture_devices()
        if devices:
            logger.warning(f"'{self._cfg.device_name}' no encontrado. Dispositivos disponibles:")
            for d in devices:
                logger.warning(f"  {d['alsa_id']}: {d['card_name']} / {d['device_name']}")
        else:
            logger.warning("No se encontraron dispositivos de captura ALSA")
        return None

    def _handle_start(self) -> None:
        if self._simulate:
            logger.info("[SIMULATE] start()")
            self._mqtt.publish_state("RUNNING", healthy=True)
            return
        if self._pipeline.state in (PipelineState.STOPPED, PipelineState.ERROR):
            ok = self._pipeline.start(self._cfg)
            if ok:
                self._start_time = time.monotonic()
                self._mqtt.publish_state("RUNNING", healthy=True, pid=os.getpid())
                self._mqtt.publish_endpoint(self._cfg)
                self._mqtt.publish_stream_target(self._cfg, source="start")
                self._mqtt.publish_event("service_started", details={"trigger": "mqtt_command"})
            else:
                self._mqtt.publish_state("ERROR", healthy=False)
                self._mqtt.publish_event("service_start_failed", severity="error", details={"trigger": "mqtt_command"})

    def _handle_stop(self) -> None:
        if self._simulate:
            logger.info("[SIMULATE] stop()")
            self._mqtt.publish_state("STOPPED", healthy=False)
            return
        self._pipeline.stop()
        self._start_time = None
        self._mqtt.publish_state("STOPPED", healthy=False)
        self._mqtt.publish_event("service_stopped", details={"trigger": "mqtt_command"})

    def _handle_restart(self) -> None:
        if self._simulate:
            logger.info("[SIMULATE] restart()")
            self._mqtt.publish_state("RUNNING", healthy=True)
            return
        self._mqtt.publish_event("service_restarting", details={"trigger": "mqtt_command"})
        ok = self._pipeline.restart(self._cfg)
        if ok:
            self._start_time = time.monotonic()
            self._pipeline_restart_count = 0
            self._mqtt.publish_state("RUNNING", healthy=True, pid=os.getpid())
            self._mqtt.publish_endpoint(self._cfg)
            self._mqtt.publish_stream_target(self._cfg, source="restart")
        else:
            self._mqtt.publish_state("ERROR", healthy=False)

    def _handle_mute(self, muted: bool) -> None:
        if self._simulate:
            logger.info(f"[SIMULATE] {'mute' if muted else 'unmute'}()")
            self._cfg.muted = muted
            self._mqtt.publish_state("PAUSED" if muted else "RUNNING", healthy=True)
            return
        ok = self._pipeline.set_mute(muted)
        if ok:
            self._cfg.muted = muted
            status = "PAUSED" if muted else "RUNNING"
            self._mqtt.publish_state(status, healthy=True, pid=os.getpid(), uptime_s=self._uptime_seconds())
            self._mqtt.publish_config_reported(self._cfg)
            self._mqtt.publish_endpoint(self._cfg)

    def _handle_apply_config(self, delta: dict) -> None:
        logger.info(f"Aplicando config delta: {delta}")
        hot_fields = {"gain_db", "muted"}
        try:
            new_cfg = self._cfg.apply_delta(delta)
        except (TypeError, ValueError) as e:
            logger.error(f"Delta inválido: {e}")
            self._mqtt.publish_event("config_apply_failed", severity="error", details={"error": str(e), "delta": delta})
            return

        errors = new_cfg.validate()
        if errors:
            logger.error(f"Config resultante inválida: {errors}")
            self._mqtt.publish_event("config_apply_failed", severity="error", details={"errors": errors, "delta": delta})
            return

        hot_changes = {k: v for k, v in delta.items() if k in hot_fields}
        cold_changes = {k: v for k, v in delta.items() if k not in hot_fields}

        if "gain_db" in hot_changes and not self._simulate:
            self._pipeline.set_gain(new_cfg.gain_db)
        if "muted" in hot_changes:
            self._handle_mute(new_cfg.muted)

        if "rode_mode" in cold_changes and not self._simulate:
            try:
                with RodeController() as rode:
                    if rode.is_available():
                        rode.set_mode(new_cfg.rode_mode)
                        logger.info(f"RODE actualizado en caliente a {new_cfg.rode_mode}")
            except Exception as e:
                logger.warning(f"No se pudo aplicar rode_mode en caliente: {e}")

        self._cfg = new_cfg
        self._mqtt._cfg = new_cfg
        if self._config_path:
            self._cfg.save(self._config_path)
        else:
            self._cfg.save()

        if cold_changes and not self._simulate:
            logger.info(f"Reiniciando pipeline por cambio de config: {list(cold_changes.keys())}")
            self._handle_restart()
        elif cold_changes and self._simulate:
            logger.info(f"[SIMULATE] Cambio de config (no reinicia pipeline): {list(cold_changes.keys())}")

        self._mqtt.publish_config_reported(self._cfg)
        self._mqtt.publish_endpoint(self._cfg)
        self._mqtt.publish_stream_target(self._cfg, source="config_applied")
        self._mqtt.publish_event("config_applied", details={"delta": delta})
        logger.info(f"Config aplicada: {delta}")

    def _on_pipeline_state_change(self, new_state: str) -> None:
        logger.debug(f"Pipeline state → {new_state}")
        healthy = new_state in (PipelineState.RUNNING, PipelineState.PAUSED)
        self._mqtt.publish_state(status=new_state, healthy=healthy, pid=os.getpid(), uptime_s=self._uptime_seconds(), last_error=self._last_error if new_state == PipelineState.ERROR else None)

    def _on_pipeline_error(self, msg: str) -> None:
        self._last_error = msg
        self._mqtt.publish_event("pipeline_error", severity="error", details={"message": msg})

    def _get_state_dict(self) -> dict:
        return {
            "status": self._pipeline.state if not self._simulate else "RUNNING",
            "healthy": self._pipeline.state in (PipelineState.RUNNING, PipelineState.PAUSED) or self._simulate,
            "pid": os.getpid(),
            "uptime_s": self._uptime_seconds(),
            "last_error": self._last_error,
        }

    def _uptime_seconds(self) -> Optional[int]:
        if self._start_time is None:
            return None
        return int(time.monotonic() - self._start_time)


def main() -> int:
    parser = argparse.ArgumentParser(description="Servicio de captura y streaming de audio binaural (Nexor)")
    parser.add_argument("--config", "-c", default=None, help="Ruta al fichero de configuración JSON")
    parser.add_argument("--simulate", "-s", action="store_true", help="Modo simulación: no requiere hardware ni GStreamer")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Nivel de log")
    args = parser.parse_args()

    logging.getLogger().setLevel(getattr(logging, args.log_level))

    config_path = args.config
    if config_path:
        cfg = AudioCaptureConfig.load(config_path)
    else:
        cfg = AudioCaptureConfig.load_with_env_overrides()

    node_runtime = NexorNodeRuntimeConfig.load_with_env_overrides()
    runtime_errors = node_runtime.validate()
    if runtime_errors:
        for err in runtime_errors:
            logger.warning(f"Node runtime inválida: {err}")
    cfg = cfg.apply_common_overrides(node_runtime.to_audio_overrides())

    if args.simulate:
        logger.info("Modo simulación activado")

    service = AudioCaptureService(cfg, simulate=args.simulate)
    service.set_config_path(config_path)
    return service.run()


if __name__ == "__main__":
    sys.exit(main())
