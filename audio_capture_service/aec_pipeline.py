from __future__ import annotations

import logging
import socket
import threading
from typing import Callable, Optional

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GLib", "2.0")
from gi.repository import GLib, Gst  # noqa: E402

from audio_shared.aec import StereoReferenceSuppressor
from audio_shared.pcm import PcmAudioFormat, PcmFrameAccumulator, apply_gain_pcm16
from audio_shared.reference_bus import AudioReferenceBus

logger = logging.getLogger(__name__)
Gst.init(None)


class PipelineState:
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"
    ERROR = "ERROR"


class AecAudioPipeline:
    def __init__(self,
                 on_state_change: Optional[Callable[[str], None]] = None,
                 on_error: Optional[Callable[[str], None]] = None) -> None:
        self._pipeline = None
        self._appsink = None
        self._loop = None
        self._loop_thread = None
        self._state = PipelineState.STOPPED
        self._sock = None
        self._cfg = None
        self._format: Optional[PcmAudioFormat] = None
        self._accumulator: Optional[PcmFrameAccumulator] = None
        self._reference_bus: Optional[AudioReferenceBus] = None
        self._suppressor: Optional[StereoReferenceSuppressor] = None
        self._muted = False
        self._gain_linear = 1.0
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
        self._cfg = cfg
        self._muted = bool(cfg.muted)
        self._gain_linear = float(cfg.gain_linear)
        self._format = PcmAudioFormat(
            sample_rate=cfg.sample_rate,
            channels=cfg.channels,
            bit_depth=16,
            frame_ms=cfg.aec_frame_ms,
        )
        self._accumulator = PcmFrameAccumulator(self._format.frame_bytes)
        self._suppressor = StereoReferenceSuppressor(
            strength=cfg.aec_strength,
            max_gain=cfg.aec_max_gain,
            smoothing=cfg.aec_smoothing,
            search_frames=cfg.aec_search_frames,
        )

        try:
            self._reference_bus = AudioReferenceBus(
                path=cfg.aec_reference_bus_path,
                audio_format=PcmAudioFormat(
                    sample_rate=cfg.sample_rate,
                    channels=1,
                    bit_depth=16,
                    frame_ms=cfg.aec_frame_ms,
                ),
                capacity_frames=max(16, cfg.aec_search_frames * 4),
            )
        except Exception as e:
            logger.error(f"No se pudo abrir el reference bus: {e}")
            self._set_state(PipelineState.ERROR)
            self._on_error(str(e))
            return False

        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        except OSError as e:
            logger.error(f"No se pudo crear socket UDP: {e}")
            self._set_state(PipelineState.ERROR)
            self._on_error(str(e))
            return False

        try:
            pipeline_str = self._build_pipeline_string(cfg)
            logger.info(f"AEC capture pipeline: {pipeline_str}")
            pipeline = Gst.parse_launch(pipeline_str)
            appsink = pipeline.get_by_name("capture_sink")
            if appsink is None:
                raise RuntimeError("appsink 'capture_sink' no encontrado")

            appsink.set_property("emit-signals", True)
            appsink.set_property("sync", False)
            appsink.set_property("max-buffers", 16)
            appsink.set_property("drop", True)
            appsink.connect("new-sample", self._on_new_sample)

            bus = pipeline.get_bus()
            bus.add_signal_watch()
            bus.connect("message", self._on_bus_message)

            ret = pipeline.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError("Pipeline AEC no pudo arrancar")

            self._pipeline = pipeline
            self._appsink = appsink
            self._loop = GLib.MainLoop()
            self._loop_thread = threading.Thread(target=self._loop.run, name="aec-gst-mainloop", daemon=True)
            self._loop_thread.start()
            self._set_state(PipelineState.RUNNING)
            return True
        except Exception as e:
            logger.error(f"Error arrancando AEC pipeline: {e}")
            self._cleanup_runtime()
            self._set_state(PipelineState.ERROR)
            self._on_error(str(e))
            return False

    def stop(self) -> None:
        with self._lock:
            if self._state == PipelineState.STOPPED:
                return
        self._set_state(PipelineState.STOPPING)
        self._cleanup_runtime()
        self._set_state(PipelineState.STOPPED)

    def restart(self, cfg) -> bool:
        self.stop()
        return self.start(cfg)

    def set_mute(self, muted: bool) -> bool:
        self._muted = bool(muted)
        self._set_state(PipelineState.PAUSED if muted else PipelineState.RUNNING)
        return True

    def set_gain(self, gain_db: float) -> bool:
        self._gain_linear = 10.0 ** (float(gain_db) / 20.0)
        return True

    def _build_pipeline_string(self, cfg) -> str:
        alsa_id = cfg.alsa_device_override or "hw:0,0"
        return (
            f"alsasrc device={alsa_id} buffer-time={cfg.alsa_buffer_time_us} ! "
            f"audioconvert ! audio/x-raw,format=S16LE,rate={cfg.sample_rate},channels={cfg.channels} ! "
            f"audioresample ! appsink name=capture_sink"
        )

    def _on_new_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.ERROR
        buffer = sample.get_buffer()
        success, map_info = buffer.map(Gst.MapFlags.READ)
        if not success:
            return Gst.FlowReturn.ERROR
        try:
            data = bytes(map_info.data)
        finally:
            buffer.unmap(map_info)

        try:
            frames = self._accumulator.append(data)
            for frame in frames:
                payload = self._process_frame(frame)
                self._sock.sendto(payload, (self._cfg.dest_ip, int(self._cfg.dest_port)))
        except Exception as e:
            logger.error(f"Error procesando frame AEC: {e}")
            self._set_state(PipelineState.ERROR)
            self._on_error(str(e))
            return Gst.FlowReturn.ERROR
        return Gst.FlowReturn.OK

    def _process_frame(self, frame: bytes) -> bytes:
        if self._muted:
            return b"\x00" * len(frame)
        ref_frames = self._reference_bus.read_recent(self._suppressor.search_frames)
        payload = self._suppressor.process(frame, ref_frames)
        payload = apply_gain_pcm16(payload, self._gain_linear, channels=2)
        return payload

    def _cleanup_runtime(self) -> None:
        try:
            if self._pipeline is not None:
                self._pipeline.set_state(Gst.State.NULL)
        except Exception as e:
            logger.debug(f"Error parando pipeline AEC: {e}")
        if self._loop and self._loop.is_running():
            self._loop.quit()
        if self._loop_thread and self._loop_thread.is_alive():
            self._loop_thread.join(timeout=3.0)
        try:
            if self._sock is not None:
                self._sock.close()
        except Exception:
            pass
        try:
            if self._reference_bus is not None:
                self._reference_bus.close()
        except Exception:
            pass
        self._pipeline = None
        self._appsink = None
        self._loop = None
        self._loop_thread = None
        self._sock = None
        self._reference_bus = None
        self._suppressor = None
        if self._accumulator is not None:
            self._accumulator.clear()
        self._accumulator = None

    def _on_bus_message(self, bus, message) -> None:
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

    def _set_state(self, new_state: str) -> None:
        with self._lock:
            if self._state == new_state:
                return
            old = self._state
            self._state = new_state
        logger.debug(f"AEC pipeline state: {old} → {new_state}")
        try:
            self._on_state_change(new_state)
        except Exception as e:
            logger.debug(f"on_state_change callback error: {e}")
