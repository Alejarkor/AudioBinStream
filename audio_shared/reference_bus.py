from __future__ import annotations

import fcntl
import mmap
import os
import struct
import time
from dataclasses import dataclass
from typing import Optional

from .pcm import PcmAudioFormat

MAGIC = b"NEXRAU1\0"
HEADER_STRUCT = struct.Struct("<8sIIIIQQII")
# magic, version, frame_bytes, capacity_frames, channels, sample_rate, write_seq, last_ts_ns, bit_depth, write_index
VERSION = 1


@dataclass
class ReferenceFrame:
    seq: int
    timestamp_ns: int
    pcm: bytes


class AudioReferenceBus:
    def __init__(self, path: str, audio_format: PcmAudioFormat, capacity_frames: int = 64) -> None:
        self.path = path
        self.audio_format = audio_format
        self.capacity_frames = int(capacity_frames)
        self.frame_bytes = self.audio_format.frame_bytes
        self._fd = None
        self._mmap = None
        self._open_or_create()

    def close(self) -> None:
        if self._mmap is not None:
            self._mmap.close()
            self._mmap = None
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def write_frame(self, pcm: bytes) -> None:
        pcm = bytes(pcm[: self.frame_bytes]).ljust(self.frame_bytes, b"\x00")
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        try:
            header = self._read_header()
            next_seq = header[6] + 1
            next_index = (header[9] + 1) % self.capacity_frames
            offset = HEADER_STRUCT.size + (next_index * self.frame_bytes)
            self._mmap.seek(offset)
            self._mmap.write(pcm)
            new_header = HEADER_STRUCT.pack(
                MAGIC,
                VERSION,
                self.frame_bytes,
                self.capacity_frames,
                self.audio_format.channels,
                self.audio_format.sample_rate,
                next_seq,
                time.time_ns(),
                self.audio_format.bit_depth,
                next_index,
            )
            self._mmap.seek(0)
            self._mmap.write(new_header)
            self._mmap.flush()
        finally:
            fcntl.flock(self._fd, fcntl.LOCK_UN)

    def read_latest(self) -> Optional[ReferenceFrame]:
        frames = self.read_recent(1)
        return frames[0] if frames else None

    def read_recent(self, count: int) -> list[ReferenceFrame]:
        count = max(0, min(int(count), self.capacity_frames))
        if count == 0:
            return []
        fcntl.flock(self._fd, fcntl.LOCK_SH)
        try:
            header = self._read_header()
            seq = header[6]
            ts_ns = header[7]
            write_index = header[9]
            if seq == 0:
                return []
            total_available = min(seq, self.capacity_frames)
            to_read = min(count, total_available)
            result: list[ReferenceFrame] = []
            for i in range(to_read):
                index = (write_index - i) % self.capacity_frames
                frame_seq = seq - i
                offset = HEADER_STRUCT.size + (index * self.frame_bytes)
                self._mmap.seek(offset)
                pcm = self._mmap.read(self.frame_bytes)
                result.append(ReferenceFrame(seq=frame_seq, timestamp_ns=ts_ns, pcm=pcm))
            return result
        finally:
            fcntl.flock(self._fd, fcntl.LOCK_UN)

    def _open_or_create(self) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        size = HEADER_STRUCT.size + (self.frame_bytes * self.capacity_frames)
        self._fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o666)
        current_size = os.path.getsize(self.path)
        if current_size != size:
            os.ftruncate(self._fd, size)
        self._mmap = mmap.mmap(self._fd, size)
        if not self._is_valid_header():
            self._initialize_header()

    def _is_valid_header(self) -> bool:
        self._mmap.seek(0)
        raw = self._mmap.read(HEADER_STRUCT.size)
        if len(raw) != HEADER_STRUCT.size:
            return False
        try:
            header = HEADER_STRUCT.unpack(raw)
        except struct.error:
            return False
        return (
            header[0] == MAGIC
            and header[1] == VERSION
            and header[2] == self.frame_bytes
            and header[3] == self.capacity_frames
            and header[4] == self.audio_format.channels
            and header[5] == self.audio_format.sample_rate
            and header[8] == self.audio_format.bit_depth
        )

    def _initialize_header(self) -> None:
        header = HEADER_STRUCT.pack(
            MAGIC,
            VERSION,
            self.frame_bytes,
            self.capacity_frames,
            self.audio_format.channels,
            self.audio_format.sample_rate,
            0,
            0,
            self.audio_format.bit_depth,
            0,
        )
        self._mmap.seek(0)
        self._mmap.write(header)
        self._mmap.write(b"\x00" * (self.frame_bytes * self.capacity_frames))
        self._mmap.flush()

    def _read_header(self):
        self._mmap.seek(0)
        raw = self._mmap.read(HEADER_STRUCT.size)
        return HEADER_STRUCT.unpack(raw)
