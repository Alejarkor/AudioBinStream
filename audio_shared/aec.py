from __future__ import annotations

import numpy as np

from .pcm import (
    float32_to_pcm16_stereo_bytes,
    pcm16_mono_bytes_to_float32,
    pcm16_stereo_bytes_to_float32,
)
from .reference_bus import ReferenceFrame


class StereoReferenceSuppressor:
    def __init__(self,
                 strength: float = 1.0,
                 max_gain: float = 2.5,
                 smoothing: float = 0.75,
                 search_frames: int = 6) -> None:
        self._strength = float(strength)
        self._max_gain = float(max_gain)
        self._smoothing = float(smoothing)
        self._search_frames = int(search_frames)
        self._prev_alpha = np.zeros((2,), dtype=np.float32)

    @property
    def search_frames(self) -> int:
        return self._search_frames

    def process(self, capture_pcm: bytes, reference_frames: list[ReferenceFrame]) -> bytes:
        if not capture_pcm:
            return capture_pcm

        capture = pcm16_stereo_bytes_to_float32(capture_pcm)
        if capture.shape[0] == 0:
            return capture_pcm
        if not reference_frames:
            return capture_pcm

        capture_mono = np.mean(capture, axis=1)
        ref = self._pick_best_reference(capture_mono, reference_frames)
        if ref is None:
            return capture_pcm

        ref_energy = float(np.dot(ref, ref)) + 1e-9
        out = capture.copy()

        for ch in range(2):
            chan = capture[:, ch]
            alpha = float(np.dot(chan, ref) / ref_energy)
            alpha = max(0.0, min(alpha, self._max_gain))
            alpha = (self._smoothing * float(self._prev_alpha[ch])) + ((1.0 - self._smoothing) * alpha)
            self._prev_alpha[ch] = alpha
            out[:, ch] = chan - (self._strength * alpha * ref)

        return float32_to_pcm16_stereo_bytes(out)

    def _pick_best_reference(self, capture_mono: np.ndarray, reference_frames: list[ReferenceFrame]) -> np.ndarray | None:
        best_score = -1.0
        best_ref = None
        capture_norm = float(np.linalg.norm(capture_mono)) + 1e-9

        for frame in reference_frames[: self._search_frames]:
            ref = pcm16_mono_bytes_to_float32(frame.pcm)
            if ref.shape[0] != capture_mono.shape[0]:
                min_len = min(ref.shape[0], capture_mono.shape[0])
                if min_len == 0:
                    continue
                ref = ref[:min_len]
                local_capture = capture_mono[:min_len]
            else:
                local_capture = capture_mono

            ref_norm = float(np.linalg.norm(ref)) + 1e-9
            score = abs(float(np.dot(local_capture, ref)) / (capture_norm * ref_norm))
            if score > best_score:
                best_score = score
                best_ref = ref

        return best_ref
