"""
pipeline.py — Pipeline GStreamer para captura y streaming de audio.

Modos soportados:
  - raw_udp    : PCM en bruto sobre UDP hacia un destino configurado.
  - rtp        : RTP con rtpL24pay/rtpL16pay sobre UDP hacia un destino
                 configurado.
  - tcp_server : compatibilidad opcional. No era el modo preferido.
"""

import logging
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class PipelineState:
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"
    ERROR = "ERROR"


class AudioPipeline:
    def __init__(self,
                 on_state_change: Optional[Callable[[str], None]] = None,
                 on_error: Optional[Callable[[str], None]] = None) -> None:
        self._pipeline = None
        self._loop = None
        self._loop_thread = None
        self._state = PipelineState.STOPPED
        self._vol_element = None
        self._lock = threading.Lock()
        self._on_state_change = on_state_change or (lambda s: None)
        self._on_error = on_error or (lambda m: None)

    @property
    def state(self) -> str:
        return self._state

    def start(self, cfg) -> bool:
        with self._lock:
            if self._state not in (PipelineState.STOPPED, PipelineState.ERROR):
                logger.warning(f"start() ignorado — estado actual: {self._state}")
                return False

        self._set_state(PipelineState.STARTING)

        try:
            import gi
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst, GLib
            Gst.init(None)
        except ImportError:
            logger.error("PyGObject / GStreamer Python bindings no disponibles. Instalar: sudo apt install python3-gi python3-gst-1.0")
            self._set_state(PipelineState.ERROR)
            return False

        pipeline_str = self._build_pipeline_string(cfg)
        logger.info(f"Pipeline: {pipeline_str}")

        try:
            pipeline = Gst.parse_launch(pipeline_str)
        except Exception as e:
            logger.error(f"Error parseando pipeline GStreamer: {e}")
            self._set_state(PipelineState.ERROR)
            return False

        vol = pipeline.get_by_name("vol")
        if vol is None:
            logger.warning("Elemento 'vol' no encontrado en el pipeline")

        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

        ret = pipeline.set_state(Gst.State.PLAYING)
        from gi.repository import Gst as GstLocal
        if ret == GstLocal.StateChangeReturn.FAILURE:
            logger.error("Pipeline no pudo arrancar (set_state PLAYING falló)")
            pipeline.set_state(GstLocal.State.NULL)
            self._set_state(PipelineState.ERROR)
            return False

        with self._lock:
            self._pipeline = pipeline
            self._vol_element = vol

        loop = GLib.MainLoop()
        self._loop = loop
        self._loop_thread = threading.Thread(target=loop.run, name="gst-mainloop", daemon=True)
        self._loop_thread.start()

        self._set_state(PipelineState.RUNNING)
        logger.info(
            f"Pipeline activo: {cfg.effective_stream_host}:{cfg.effective_stream_port} "
            f"({cfg.protocol}, {cfg.sample_rate}Hz, {cfg.channels}ch, {cfg.bit_depth}bit)"
        )
        return True

    def stop(self) -> None:
        with self._lock:
            if self._state in (PipelineState.STOPPED,):
                return
            pipeline = self._pipeline
            loop = self._loop

        self._set_state(PipelineState.STOPPING)

        try:
            if pipeline:
                import gi
                gi.require_version("Gst", "1.0")
                from gi.repository import Gst
                pipeline.set_state(Gst.State.NULL)
        except Exception as e:
            logger.warning(f"Error parando pipeline: {e}")

        if loop and loop.is_running():
            loop.quit()

        if self._loop_thread and self._loop_thread.is_alive():
            self._loop_thread.join(timeout=3.0)

        with self._lock:
            self._pipeline = None
            self._vol_element = None
            self._loop = None
            self._loop_thread = None

        self._set_state(PipelineState.STOPPED)
        logger.info("Pipeline detenido")

    def restart(self, cfg) -> bool:
        logger.info("Reiniciando pipeline...")
        self.stop()
        return self.start(cfg)

    def set_mute(self, muted: bool) -> bool:
        with self._lock:
            vol = self._vol_element
        if vol is None:
            logger.warning("set_mute: elemento volume no disponible")
            return False
        vol.set_property("mute", muted)
        self._set_state(PipelineState.PAUSED if muted else PipelineState.RUNNING)
        logger.info(f"{'Muted' if muted else 'Unmuted'}")
        return True

    def set_gain(self, gain_db: float) -> bool:
        with self._lock:
            vol = self._vol_element
        if vol is None:
            logger.warning("set_gain: elemento volume no disponible")
            return False
        gain_linear = 10.0 ** (gain_db / 20.0)
        vol.set_property("volume", max(0.0, min(gain_linear, 10.0)))
        logger.info(f"Gain: {gain_db:.1f} dB (linear={gain_linear:.3f})")
        return True

    def _build_pipeline_string(self, cfg) -> str:
        alsa_id = cfg.alsa_device_override or "hw:0,0"
        queue_time_ns = cfg.pipeline_queue_ms * 1_000_000
        caps = (
            f"audio/x-raw,"
            f"format={cfg.gst_format},"
            f"rate={cfg.sample_rate},"
            f"channels={cfg.channels}"
        )
        gain_linear = max(0.0, min(cfg.gain_linear, 10.0))
        mute_str = "true" if cfg.muted else "false"
        src = f"alsasrc device={alsa_id} buffer-time={cfg.alsa_buffer_time_us}"
        convert = f"audioconvert ! {caps} ! audioresample"
        volume = f"volume name=vol volume={gain_linear:.4f} mute={mute_str}"
        queue = (
            f"queue max-size-time={queue_time_ns} "
            f"max-size-bytes=0 max-size-buffers=0"
        )

        if cfg.protocol == "rtp":
            sink = (
                f"{cfg.rtp_payloader} ! "
                f"udpsink host={cfg.dest_ip} port={cfg.dest_port} sync=false async=false"
            )
        elif cfg.protocol == "raw_udp":
            sink = (
                f"udpsink host={cfg.dest_ip} port={cfg.dest_port} sync=false async=false"
            )
        else:
            sink = (
                f"tcpserversink host={cfg.stream_bind_ip} port={cfg.stream_port} sync=false"
            )

        return f"{src} ! {convert} ! {volume} ! {queue} ! {sink}"

    def _on_bus_message(self, bus, message) -> None:
        try:
            import gi
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst

            if message.type == Gst.MessageType.ERROR:
                err, debug = message.parse_error()
                msg = f"GStreamer ERROR: {err.message} | debug: {debug}"
                logger.error(msg)
                self._set_state(PipelineState.ERROR)
                self._on_error(msg)
                if self._loop and self._loop.is_running():
                    self._loop.quit()

            elif message.type == Gst.MessageType.EOS:
                logger.info("GStreamer EOS recibido")
                self.stop()

            elif message.type == Gst.MessageType.WARNING:
                warn, debug = message.parse_warning()
                logger.warning(f"GStreamer WARNING: {warn.message}")

            elif message.type == Gst.MessageType.STATE_CHANGED:
                if message.src == self._pipeline:
                    old, new, pending = message.parse_state_changed()
                    logger.debug(
                        f"Pipeline state: {old.value_nick} → {new.value_nick}" +
                        (f" (pending: {pending.value_nick})" if pending != Gst.State.VOID_PENDING else "")
                    )
        except Exception as e:
            logger.debug(f"_on_bus_message exception: {e}")

    def _set_state(self, new_state: str) -> None:
        with self._lock:
            if self._state == new_state:
                return
            old = self._state
            self._state = new_state
        logger.debug(f"Pipeline state: {old} → {new_state}")
        try:
            self._on_state_change(new_state)
        except Exception as e:
            logger.debug(f"on_state_change callback error: {e}")
