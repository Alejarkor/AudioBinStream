"""
config.py — Gestión de configuración del servicio de captura y streaming de
audio binaural.

La configuración del servicio se persistía en JSON (desired config del propio
servicio), mientras que la configuración común del nodo se cargaba desde un
fichero general separado. De esa forma podía compartirse la conexión MQTT y la
identidad del nodo entre varios servicios Nexor.

Variables de entorno reconocidas (sobreescriben el JSON del servicio):
    AUDIO_CAPTURE_CONFIG, AUDIO_DEST_IP, AUDIO_DEST_PORT,
    STREAM_BIND_IP, STREAM_PORT, RODE_MODE

Variables de entorno del nodo común (normalmente definidas en otro fichero):
    NODE_ID, MQTT_NAMESPACE, MQTT_BROKER, MQTT_PORT, MQTT_USER,
    MQTT_PASSWORD, NEXOR_ADVERTISE_HOST
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, asdict
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = os.environ.get(
    "AUDIO_CAPTURE_CONFIG",
    "/etc/nexor/audio_capture.json"
)


@dataclass
class AudioCaptureConfig:
    # ── Transporte / streaming ────────────────────────────────────────────────
    # raw_udp    → PCM en bruto sobre UDP hacia un destino configurado.
    # rtp        → RTP sobre UDP hacia un destino configurado.
    # tcp_server → compatibilidad opcional. No era el modo preferido.
    protocol: str = "raw_udp"

    # Para protocolos de tipo push (raw_udp / rtp)
    dest_ip: str = "192.168.0.20"
    dest_port: int = 1234

    # Para protocolo servidor (tcp_server)
    stream_bind_ip: str = "0.0.0.0"
    stream_port: int = 5004

    # ── Audio ─────────────────────────────────────────────────────────────────
    sample_rate: int = 48000
    channels: int = 2
    bit_depth: int = 24
    gain_db: float = 0.0
    muted: bool = False

    # ── Dispositivo ───────────────────────────────────────────────────────────
    device_name: str = "AI-Micro"
    alsa_device_override: Optional[str] = None

    # ── RODE AI-Micro ─────────────────────────────────────────────────────────
    rode_mode: str = "SPLIT"
    rode_auto_set_mode: bool = True

    # ── Configuración común heredada del nodo ────────────────────────────────
    mqtt_broker: str = "127.0.0.1"
    mqtt_port: int = 1883
    mqtt_user: str = ""
    mqtt_password: str = ""
    mqtt_keepalive: int = 60
    mqtt_reconnect_delay: int = 5
    node_id: str = "nexor-01"
    mqtt_namespace: str = "nexor/v1"
    advertise_host: str = "127.0.0.1"

    # ── Pipeline ──────────────────────────────────────────────────────────────
    alsa_buffer_time_us: int = 5000
    pipeline_queue_ms: int = 5

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AudioCaptureConfig":
        known = set(cls.__dataclass_fields__.keys())
        return cls(**{k: v for k, v in d.items() if k in known})

    @staticmethod
    def normalize_rode_mode(value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("rode_mode debe ser string")
        norm = value.strip().lower()
        mapping = {
            "split": "SPLIT",
            "merged": "MERGED",
            "merge": "MERGED",
            "stereo": "STEREO",
        }
        if norm not in mapping:
            raise ValueError(f"rode_mode inválido: {value}")
        return mapping[norm]

    @property
    def mqtt_rode_mode(self) -> str:
        return {
            "SPLIT": "split",
            "MERGED": "merged",
            "STEREO": "stereo",
        }.get(self.rode_mode.upper(), self.rode_mode.lower())

    @property
    def is_push_transport(self) -> bool:
        return self.protocol in ("raw_udp", "rtp")

    def apply_delta(self, delta: dict) -> "AudioCaptureConfig":
        current = self.to_dict()
        merged = dict(current)
        merged.update(delta)
        if "rode_mode" in merged:
            merged["rode_mode"] = self.normalize_rode_mode(merged["rode_mode"])
        return self.from_dict(merged)

    def apply_common_overrides(self, overrides: dict) -> "AudioCaptureConfig":
        return self.apply_delta(overrides)

    def save(self, path: str = DEFAULT_CONFIG_PATH) -> None:
        path = path or DEFAULT_CONFIG_PATH
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, indent=2)
            os.replace(tmp, path)
            logger.info(f"Config guardada en {path}")
        except OSError as e:
            logger.error(f"Error guardando config en {path}: {e}")

    @classmethod
    def load(cls, path: str = DEFAULT_CONFIG_PATH) -> "AudioCaptureConfig":
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            cfg = cls.from_dict(data)
            if cfg.rode_mode:
                cfg.rode_mode = cls.normalize_rode_mode(cfg.rode_mode)
            logger.info(f"Config cargada desde {path}")
            return cfg
        except FileNotFoundError:
            logger.info(f"Config no encontrada en {path} — usando valores por defecto")
            return cls()
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            logger.warning(f"Error parseando config en {path}: {e} — usando valores por defecto")
            return cls()

    @classmethod
    def load_with_env_overrides(cls, path: str = DEFAULT_CONFIG_PATH) -> "AudioCaptureConfig":
        cfg = cls.load(path)
        env_map = {
            "AUDIO_DEST_IP": ("dest_ip", str),
            "AUDIO_DEST_PORT": ("dest_port", int),
            "STREAM_BIND_IP": ("stream_bind_ip", str),
            "STREAM_PORT": ("stream_port", int),
            "RODE_MODE": ("rode_mode", cls.normalize_rode_mode),
        }
        delta = {}
        for env_key, (field_name, cast) in env_map.items():
            val = os.environ.get(env_key)
            if val is None:
                continue
            try:
                delta[field_name] = cast(val)
            except (ValueError, TypeError) as e:
                logger.warning(f"Variable de entorno {env_key}={val!r} inválida: {e}")
        if delta:
            cfg = cfg.apply_delta(delta)
        return cfg

    @property
    def gain_linear(self) -> float:
        return 10.0 ** (self.gain_db / 20.0)

    @property
    def gst_format(self) -> str:
        return "S24LE" if self.bit_depth == 24 else "S16LE"

    @property
    def rtp_payloader(self) -> str:
        return "rtpL24pay" if self.bit_depth == 24 else "rtpL16pay"

    @property
    def mqtt_node_base_topic(self) -> str:
        return f"{self.mqtt_namespace}/nodes/{self.node_id}"

    @property
    def mqtt_base_topic(self) -> str:
        return f"{self.mqtt_node_base_topic}/services/audio_binaural"

    @property
    def mqtt_cmd_topic(self) -> str:
        return f"{self.mqtt_base_topic}/cmd"

    @property
    def mqtt_state_topic(self) -> str:
        return f"{self.mqtt_base_topic}/state"

    @property
    def mqtt_config_desired_topic(self) -> str:
        return f"{self.mqtt_base_topic}/config/desired"

    @property
    def mqtt_config_reported_topic(self) -> str:
        return f"{self.mqtt_base_topic}/config/reported"

    @property
    def mqtt_events_topic(self) -> str:
        return f"{self.mqtt_base_topic}/events"

    @property
    def mqtt_capabilities_topic(self) -> str:
        return f"{self.mqtt_base_topic}/capabilities"

    @property
    def mqtt_endpoint_topic(self) -> str:
        return f"{self.mqtt_base_topic}/endpoint"

    @property
    def mqtt_stream_target_desired_topic(self) -> str:
        return f"{self.mqtt_base_topic}/stream_target/desired"

    @property
    def mqtt_stream_target_reported_topic(self) -> str:
        return f"{self.mqtt_base_topic}/stream_target/reported"

    @property
    def effective_stream_host(self) -> str:
        if self.protocol == "tcp_server":
            return self.advertise_host
        return self.dest_ip

    @property
    def effective_stream_port(self) -> int:
        if self.protocol == "tcp_server":
            return self.stream_port
        return self.dest_port

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.bit_depth not in (16, 24):
            errors.append(f"bit_depth inválido: {self.bit_depth} (válidos: 16, 24)")
        if self.sample_rate not in (8000, 16000, 32000, 44100, 48000, 96000):
            errors.append(f"sample_rate inusual: {self.sample_rate}")
        if self.channels not in (1, 2):
            errors.append(f"channels inválido: {self.channels} (válidos: 1, 2)")
        if not (-60.0 <= self.gain_db <= 60.0):
            errors.append(f"gain_db fuera de rango: {self.gain_db}")
        if self.protocol not in ("rtp", "raw_udp", "tcp_server"):
            errors.append(f"protocol inválido: {self.protocol}")
        try:
            self.normalize_rode_mode(self.rode_mode)
        except ValueError as e:
            errors.append(str(e))
        if self.protocol == "tcp_server":
            if not (1 <= self.stream_port <= 65535):
                errors.append(f"stream_port inválido: {self.stream_port}")
            if not self.stream_bind_ip:
                errors.append("stream_bind_ip vacío")
        else:
            if not self.dest_ip:
                errors.append("dest_ip vacío")
            if not (1 <= self.dest_port <= 65535):
                errors.append(f"dest_port inválido: {self.dest_port}")
        if not self.node_id:
            errors.append("node_id vacío")
        if not self.mqtt_namespace:
            errors.append("mqtt_namespace vacío")
        return errors
