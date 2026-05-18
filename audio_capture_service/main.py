"""
main.py — Servicio de captura y streaming de audio (Nexor / Jetson Orin)
"""

import argparse
import logging
import os
import signal
import sys
import time
import threading
from typing import Optional

from .aec_pipeline import AecAudioPipeline, PipelineState as AecPipelineState
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
        self._waiting_for_target = False
        self._stream_target_confirmed = False
        self._idle = False

        if cfg.aec_enabled and not simulate:
            self._pipeline = AecAudioPipeline(
                on_state_change=self._on_pipeline_state_change,
                on_error=self._on_pipeline_error,
            )
            self._pipeline_state_enum = AecPipelineState
            logger.info("AEC compartido activado para audio_binaural")
        else:
            self._pipeline = AudioPipeline(
                on_state_change=self._on_pipeline_state_change,
                on_error=self._on_pipeline_error,
            )
            self._pipeline_state_enum = PipelineState

        self._mqtt = AudioCaptureServiceAdapter(
            cfg=cfg,
            on_start=self._handle_start,
            on_resume=self._handle_resume,
            on_standby=self._handle_standby,
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
        logger.info(f"  AEC compartido: {'on' if self._cfg.aec_enabled else 'off'}")
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
        self._mqtt.publish_event("service_starting", details={"simulate": self._simulate, "aec_enabled": self._cfg.aec_enabled})

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

        self._mqtt.publish_config_reported(self._cfg)
        self._mqtt.publish_endpoint(self._cfg)
        self._mqtt.publish_stream_target(self._cfg, source="startup_unconfirmed")

        if self._requires_stream_target() and not self._simulate:
            self._waiting_for_target = True
            self._stream_target_confirmed = False
            self._idle = False
            self._mqtt.publish_state("WAITING_TARGET", healthy=True, pid=os.getpid())
            self._mqtt.publish_event(
                "waiting_for_target",
                details={
                    "reason": "stream_target_not_confirmed",
                    "current_dest_ip": self._cfg.dest_ip,
                    "current_dest_port": self._cfg.dest_port,
                },
            )
            logger.info("Servicio en WAITING_TARGET — esperando stream_target/desired por MQTT")
        else:
            if not self._start_pipeline(trigger="startup"):
                self._mqtt.stop()
                return 1

        logger.info("Servicio corriendo. Esperando shutdown...")

        try:
            while not self._shutdown_event.is_set():
                self._shutdown_event.wait(timeout=5.0)
                if self._mqtt.is_connected():
                    self._mqtt.publish_state(
                        status=self._runtime_status(),
                        healthy=self._runtime_healthy(),
                        pid=os.getpid(),
                        uptime_s=self._uptime_seconds(),
                        last_error=self._last_error if self._runtime_status() == self._pipeline_state_enum.ERROR else None,
                    )

                if not self._simulate and not self._shutdown_event.is_set():
                    if self._pipeline.state == self._pipeline_state_enum.ERROR:
                        if self._pipeline_restart_count < self._max_pipeline_restarts:
                            self._pipeline_restart_count += 1
                            logger.warning(f"Reintentando pipeline (intento {self._pipeline_restart_count})")
                            time.sleep(3.0)
                            if self._waiting_for_target and not self._stream_target_confirmed:
                                logger.info("Pipeline en ERROR pero el servicio sigue en WAITING_TARGET")
                            elif self._idle:
                                logger.info("Pipeline en ERROR pero el servicio sigue en IDLE")
                            else:
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

    def _requires_stream_target(self) -> bool:
        return self._cfg.is_push_transport

    def _runtime_status(self) -> str:
        if self._waiting_for_target:
            return "WAITING_TARGET"
        if self._idle:
            return "IDLE"
        if self._simulate:
            return "RUNNING"
        return self._pipeline.state

    def _runtime_healthy(self) -> bool:
        if self._waiting_for_target or self._idle:
            return True
        if self._simulate:
            return True
        return self._pipeline.state in (self._pipeline_state_enum.RUNNING, self._pipeline_state_enum.PAUSED)

    def _start_pipeline(self, trigger: str) -> bool:
        if self._simulate:
            logger.info(f"[SIMULATE] start_pipeline(trigger={trigger})")
            self._start_time = time.monotonic()
            self._waiting_for_target = False
            self._idle = False
            self._mqtt.publish_state("RUNNING", healthy=True, pid=os.getpid())
            self._mqtt.publish_config_reported(self._cfg)
            self._mqtt.publish_endpoint(self._cfg)
            self._mqtt.publish_stream_target(self._cfg, source=trigger)
            self._mqtt.publish_event("service_started", details={"trigger": trigger, "pid": os.getpid()})
            return True

        ok = self._pipeline.start(self._cfg)
        if not ok:
            logger.error("Pipeline no pudo arrancar")
            self._mqtt.publish_state("ERROR", healthy=False, last_error="Pipeline start failed")
            self._mqtt.publish_event("service_start_failed", severity="error", details={"reason": "pipeline_start_failed", "trigger": trigger})
            return False

        self._start_time = time.monotonic()
        self._pipeline_restart_count = 0
        self._waiting_for_target = False
        self._idle = False
        self._mqtt.publish_state("RUNNING", healthy=True, pid=os.getpid())
        self._mqtt.publish_config_reported(self._cfg)
        self._mqtt.publish_endpoint(self._cfg)
        self._mqtt.publish_stream_target(self._cfg, source=trigger)
        self._mqtt.publish_event("service_started", details={"trigger": trigger, "pid": os.getpid()})
        return True

    def _restart_pipeline(self, trigger: str) -> bool:
        if self._simulate:
            logger.info(f"[SIMULATE] restart_pipeline(trigger={trigger})")
            self._start_time = time.monotonic()
            self._pipeline_restart_count = 0
            self._waiting_for_target = False
            self._idle = False
            self._mqtt.publish_state("RUNNING", healthy=True, pid=os.getpid())
            self._mqtt.publish_config_reported(self._cfg)
            self._mqtt.publish_endpoint(self._cfg)
            self._mqtt.publish_stream_target(self._cfg, source=trigger)
            self._mqtt.publish_event("service_restarted", details={"trigger": trigger, "pid": os.getpid()})
            return True

        self._mqtt.publish_event("service_restarting", details={"trigger": trigger})
        ok = self._pipeline.restart(self._cfg)
        if not ok:
            self._mqtt.publish_state("ERROR", healthy=False)
            self._mqtt.publish_event("service_restart_failed", severity="error", details={"trigger": trigger})
            return False

        self._start_time = time.monotonic()
        self._pipeline_restart_count = 0
        self._waiting_for_target = False
        self._idle = False
        self._mqtt.publish_state("RUNNING", healthy=True, pid=os.getpid())
        self._mqtt.publish_config_reported(self._cfg)
        self._mqtt.publish_endpoint(self._cfg)
        self._mqtt.publish_stream_target(self._cfg, source=trigger)
        self._mqtt.publish_event("service_restarted", details={"trigger": trigger, "pid": os.getpid()})
        return True

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
        self._handle_resume()

    def _handle_resume(self) -> None:
        if self._simulate:
            logger.info("[SIMULATE] resume()")
            self._waiting_for_target = False
            self._idle = False
            self._mqtt.publish_state("RUNNING", healthy=True)
            return

        if self._waiting_for_target and not self._stream_target_confirmed:
            logger.info("resume() ignorado — sigue en WAITING_TARGET")
            self._mqtt.publish_state("WAITING_TARGET", healthy=True, pid=os.getpid())
            self._mqtt.publish_event("service_resume_blocked", severity="warning", details={"reason": "stream_target_not_confirmed"})
            return

        if self._idle:
            logger.info("Reanudando servicio desde IDLE")
            if self._pipeline.state in (self._pipeline_state_enum.STOPPED, self._pipeline_state_enum.ERROR):
                self._start_pipeline(trigger="resume_command")
            else:
                self._idle = False
                self._mqtt.publish_state("RUNNING", healthy=True, pid=os.getpid())
                self._mqtt.publish_event("service_resumed", details={"trigger": "resume_command", "pid": os.getpid()})
            return

        if self._pipeline.state in (self._pipeline_state_enum.STOPPED, self._pipeline_state_enum.ERROR):
            self._start_pipeline(trigger="start_command")

    def _handle_standby(self) -> None:
        if self._simulate:
            logger.info("[SIMULATE] standby()")
            self._idle = True
            self._waiting_for_target = False
            self._mqtt.publish_state("IDLE", healthy=True)
            self._mqtt.publish_event("service_standby", details={"trigger": "mqtt_command"})
            return

        if self._pipeline.state not in (self._pipeline_state_enum.STOPPED,):
            self._pipeline.stop()

        self._start_time = None
        self._idle = True
        self._waiting_for_target = False
        self._mqtt.publish_state("IDLE", healthy=True, pid=os.getpid())
        self._mqtt.publish_config_reported(self._cfg)
        self._mqtt.publish_endpoint(self._cfg)
        self._mqtt.publish_stream_target(self._cfg, source="standby")
        self._mqtt.publish_event("service_standby", details={"trigger": "mqtt_command", "target_confirmed": self._stream_target_confirmed})

    def _handle_stop(self) -> None:
        if self._simulate:
            logger.info("[SIMULATE] stop()")
            self._idle = False
            self._waiting_for_target = False
            self._mqtt.publish_state("STOPPED", healthy=False)
            return
        self._pipeline.stop()
        self._start_time = None
        self._idle = False
        self._waiting_for_target = False
        self._mqtt.publish_state("STOPPED", healthy=False)
        self._mqtt.publish_event("service_stopped", details={"trigger": "mqtt_command"})

    def _handle_restart(self) -> None:
        if self._simulate:
            logger.info("[SIMULATE] restart()")
            self._idle = False
            self._waiting_for_target = False
            self._mqtt.publish_state("RUNNING", healthy=True)
            return

        if self._waiting_for_target and not self._stream_target_confirmed:
            logger.info("restart() ignorado — sigue en WAITING_TARGET")
            self._mqtt.publish_state("WAITING_TARGET", healthy=True, pid=os.getpid())
            self._mqtt.publish_event("service_restart_blocked", severity="warning", details={"reason": "stream_target_not_confirmed"})
            return

        self._restart_pipeline(trigger="mqtt_command")

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
        target_fields = {"dest_ip", "dest_port", "protocol"}
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
        target_changes = {k: v for k, v in delta.items() if k in target_fields}

        if target_changes and new_cfg.is_push_transport:
            self._stream_target_confirmed = True
            logger.info(f"Destino de stream confirmado por MQTT: {target_changes}")

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

        if self._waiting_for_target and self._stream_target_confirmed:
            logger.info("Destino confirmado mientras el servicio estaba en WAITING_TARGET — arrancando pipeline")
            if not self._start_pipeline(trigger="stream_target_confirmed"):
                self._mqtt.publish_event("target_confirmed_but_start_failed", severity="error", details={"delta": delta})
                return
        elif cold_changes and not self._simulate:
            if self._pipeline.state in (self._pipeline_state_enum.RUNNING, self._pipeline_state_enum.PAUSED):
                logger.info(f"Reiniciando pipeline por cambio de config: {list(cold_changes.keys())}")
                self._restart_pipeline(trigger="config_applied")
            elif self._pipeline.state in (self._pipeline_state_enum.STOPPED, self._pipeline_state_enum.ERROR) and not self._waiting_for_target and not self._idle and self._stream_target_confirmed:
                logger.info("Config aplicada con pipeline parado y target confirmado — arrancando pipeline")
                self._start_pipeline(trigger="config_applied")
            elif self._idle:
                logger.info(f"Config aplicada mientras el servicio estaba en IDLE: {list(cold_changes.keys())}")
        elif cold_changes and self._simulate:
            logger.info(f"[SIMULATE] Cambio de config (no reinicia pipeline): {list(cold_changes.keys())}")

        if self._waiting_for_target and not self._stream_target_confirmed:
            self._mqtt.publish_state("WAITING_TARGET", healthy=True, pid=os.getpid())
        elif self._idle:
            self._mqtt.publish_state("IDLE", healthy=True, pid=os.getpid())

        self._mqtt.publish_config_reported(self._cfg)
        self._mqtt.publish_endpoint(self._cfg)
        self._mqtt.publish_stream_target(self._cfg, source="config_applied")
        self._mqtt.publish_event("config_applied", details={"delta": delta})
        logger.info(f"Config aplicada: {delta}")

    def _on_pipeline_state_change(self, new_state: str) -> None:
        logger.debug(f"Pipeline state → {new_state}")
        if self._waiting_for_target:
            status = "WAITING_TARGET"
            healthy = True
        elif self._idle:
            status = "IDLE"
            healthy = True
        else:
            status = new_state
            healthy = new_state in (self._pipeline_state_enum.RUNNING, self._pipeline_state_enum.PAUSED)
        self._mqtt.publish_state(status=status, healthy=healthy, pid=os.getpid(), uptime_s=self._uptime_seconds(), last_error=self._last_error if status == self._pipeline_state_enum.ERROR else None)

    def _on_pipeline_error(self, msg: str) -> None:
        self._last_error = msg
        self._mqtt.publish_event("pipeline_error", severity="error", details={"message": msg})

    def _get_state_dict(self) -> dict:
        return {
            "status": self._runtime_status(),
            "healthy": self._runtime_healthy(),
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
