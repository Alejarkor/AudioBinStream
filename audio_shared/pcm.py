from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class PcmAudioFormat:
    sample_rate: int = 48000
    channels: int = 1
    bit_depth: int = 16
    frame_ms: int = 10

    @property
    def bytes_per_sample(self) -> int:
        return 3 if self.bit_depth == 24 else 2

    @property
    def frame_samples_per_channel(self) -> int:
        return int(self.sample_rate * self.frame_ms / 1000)

    @property
    def frame_total_samples(self) -> int:
        return self.frame_samples_per_channel * self.channels

    @property
    def frame_bytes(self) -> int:
        return self.frame_total_samples * self.bytes_per_sample


class PcmFrameAccumulator:
    def __init__(self, frame_bytes: int) -> None:
        self._frame_bytes = int(frame_bytes)
        self._buffer = bytearray()

    def append(self, data: bytes) -> list[bytes]:
        if not data:
            return []
        self._buffer.extend(data)
        frames: list[bytes] = []
        while len(self._buffer) >= self._frame_bytes:
            frames.append(bytes(self._buffer[: self._frame_bytes]))
            del self._buffer[: self._frame_bytes]
        return frames

    def clear(self) -> None:
        self._buffer.clear()


def pcm16_mono_bytes_to_float32(data: bytes) -> np.ndarray:
    if not data:
        return np.zeros((0,), dtype=np.float32)
    arr = np.frombuffer(data, dtype='<i2').astype(np.float32)
    return arr / 32768.0


def pcm16_stereo_bytes_to_float32(data: bytes) -> np.ndarray:
    if not data:
        return np.zeros((0, 2), dtype=np.float32)
    arr = np.frombuffer(data, dtype='<i2').astype(np.float32)
    arr = arr.reshape(-1, 2)
    return arr / 32768.0


def float32_to_pcm16_mono_bytes(samples: np.ndarray) -> bytes:
    clipped = np.clip(samples, -1.0, 1.0)
    pcm = np.round(clipped * 32767.0).astype('<i2')
    return pcm.tobytes()


def float32_to_pcm16_stereo_bytes(samples: np.ndarray) -> bytes:
    clipped = np.clip(samples, -1.0, 1.0)
    pcm = np.round(clipped * 32767.0).astype('<i2')
    return pcm.tobytes()


def apply_gain_pcm16(data: bytes, gain_linear: float, channels: int) -> bytes:
    if not data:
        return data
    if channels not in (1, 2):
        raise ValueError(f'channels no soportado: {channels}')
    arr = np.frombuffer(data, dtype='<i2').astype(np.float32)
    arr *= float(gain_linear)
    arr = np.clip(arr, -32768.0, 32767.0).astype('<i2')
    return arr.tobytes()


def downmix_pcm16_to_mono(data: bytes, channels: int) -> bytes:
    if channels == 1:
        return data
    if channels != 2:
        raise ValueError(f'channels no soportado para downmix: {channels}')
    arr = np.frombuffer(data, dtype='<i2').astype(np.int32).reshape(-1, 2)
    mono = ((arr[:, 0] + arr[:, 1]) // 2).astype('<i2')
    return mono.tobytes()
