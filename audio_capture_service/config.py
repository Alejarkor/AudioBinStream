"""Configuración única y validada del servicio Audio Binaural."""

from __future__ import annotations

import ipaddress
import json
import os
from dataclasses import asdict, dataclass, replace
from pathlib import Path

DEFAULT_CONFIG_PATH = "/etc/nexor/audio-binaural.json"
PUBLIC_REPORTED_CONFIG_EXCLUDE_KEYS = {"mqtt_user", "mqtt_password"}
RUNTIME_FIELDS = {
    "protocol", "dest_ip", "dest_port", "sample_rate", "channels",
    "bit_depth", "gain_db", "muted", "rode_mode",
}


@dataclass(frozen=True)
class AudioCaptureConfig:
    """Configuración estática; MQTT solo altera campos operativos en memoria."""

    schema_version: int = 1
    node_id: str = "nexor-01"
    mqtt_namespace: str = "nexor/v1"
    mqtt_broker: str = "127.0.0.1"
    mqtt_port: int = 1883
    mqtt_user: str = ""
    mqtt_password: str = ""
    mqtt_keepalive: int = 60
    mqtt_reconnect_min_seconds: int = 2
    mqtt_reconnect_max_seconds: int = 60
    mqtt_tls_enabled: bool = False
    mqtt_tls_ca_file: str | None = None
    advertise_host: str = ""
    protocol: str = "raw_udp"
    dest_ip: str = ""
    dest_port: int = 0
    stream_bind_ip: str = "0.0.0.0"
    stream_port: int = 5004
    wait_for_mqtt_target: bool = True
    sample_rate: int = 48000
    channels: int = 2
    bit_depth: int = 24
    gain_db: float = 0.0
    muted: bool = False
    device_name: str = "AI-Micro"
    alsa_device_override: str | None = None
    rode_mode: str = "SPLIT"
    rode_auto_set_mode: bool = True
    alsa_buffer_time_us: int = 10000
    pipeline_queue_ms: int = 10
    heartbeat_seconds: int = 5

    def to_dict(self) -> dict:
        return asdict(self)

    def to_report_dict(self) -> dict:
        data = self.to_dict()
        for key in PUBLIC_REPORTED_CONFIG_EXCLUDE_KEYS:
            data.pop(key, None)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "AudioCaptureConfig":
        known = set(cls.__dataclass_fields__)
        return cls(**{key: value for key, value in data.items() if key in known})

    @classmethod
    def load(cls, path: str) -> "AudioCaptureConfig":
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValueError(f"No existe el archivo de configuración: {path}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON inválido en {path}: {exc}") from exc
        cfg = cls.from_dict(data).with_shared_mqtt_environment()
        errors = cfg.validate(allow_missing_target=True)
        if errors:
            raise ValueError("Configuración inválida: " + "; ".join(errors))
        return cfg

    def with_shared_mqtt_environment(self) -> "AudioCaptureConfig":
        """Obtiene la conexión MQTT únicamente de /etc/nexor/mqtt.env."""
        def required(name: str) -> str:
            value = os.environ.get(name, "").strip()
            if not value:
                raise ValueError(f"Falta {name} en /etc/nexor/mqtt.env")
            return value

        def optional_int(name: str, default: int) -> int:
            value = os.environ.get(name)
            return default if value in (None, "") else int(value)

        def optional_bool(name: str, default: bool) -> bool:
            value = os.environ.get(name)
            if value in (None, ""):
                return default
            if value.lower() in {"1", "true", "yes"}:
                return True
            if value.lower() in {"0", "false", "no"}:
                return False
            raise ValueError(f"{name} debe ser true o false")

        try:
            return replace(
                self,
                node_id=required("NEXOR_NODE_ID"),
                mqtt_namespace=required("NEXOR_MQTT_NAMESPACE"),
                mqtt_broker=required("NEXOR_MQTT_HOST"),
                mqtt_port=int(required("NEXOR_MQTT_PORT")),
                mqtt_user=os.environ.get("NEXOR_MQTT_USERNAME", ""),
                mqtt_password=os.environ.get("NEXOR_MQTT_PASSWORD", ""),
                mqtt_keepalive=optional_int("NEXOR_MQTT_KEEPALIVE_SECONDS", self.mqtt_keepalive),
                mqtt_reconnect_min_seconds=optional_int("NEXOR_MQTT_RECONNECT_MIN_SECONDS", self.mqtt_reconnect_min_seconds),
                mqtt_reconnect_max_seconds=optional_int("NEXOR_MQTT_RECONNECT_MAX_SECONDS", self.mqtt_reconnect_max_seconds),
                mqtt_tls_enabled=optional_bool("NEXOR_MQTT_TLS_ENABLED", self.mqtt_tls_enabled),
                mqtt_tls_ca_file=os.environ.get("NEXOR_MQTT_TLS_CA_FILE") or None,
            )
        except ValueError as exc:
            raise ValueError(f"Configuración MQTT compartida inválida: {exc}") from exc

    @staticmethod
    def normalize_rode_mode(value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("rode_mode debe ser texto")
        modes = {"split": "SPLIT", "merged": "MERGED", "merge": "MERGED", "stereo": "STEREO"}
        try:
            return modes[value.strip().lower()]
        except KeyError as exc:
            raise ValueError(f"rode_mode inválido: {value}") from exc

    @property
    def mqtt_rode_mode(self) -> str:
        return self.normalize_rode_mode(self.rode_mode).lower()

    @property
    def is_push_transport(self) -> bool:
        return self.protocol in {"raw_udp", "rtp"}

    @property
    def has_stream_target(self) -> bool:
        return bool(self.dest_ip) and 1 <= self.dest_port <= 65535

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
        return f"{self.mqtt_node_base_topic}/services/audio_binaural_streaming"

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
        return self.advertise_host if self.protocol == "tcp_server" else self.dest_ip

    @property
    def effective_stream_port(self) -> int:
        return self.stream_port if self.protocol == "tcp_server" else self.dest_port

    def apply_runtime_delta(self, delta: dict) -> "AudioCaptureConfig":
        unknown = set(delta) - RUNTIME_FIELDS
        if unknown:
            raise ValueError("Campos no permitidos por MQTT: " + ", ".join(sorted(unknown)))
        values = self.to_dict()
        values.update(delta)
        values["rode_mode"] = self.normalize_rode_mode(values["rode_mode"])
        cfg = self.from_dict(values)
        target_changed = bool({"protocol", "dest_ip", "dest_port"} & set(delta))
        errors = cfg.validate(allow_missing_target=not target_changed)
        if errors:
            raise ValueError("Configuración dinámica inválida: " + "; ".join(errors))
        return cfg

    def validate(self, allow_missing_target: bool = True) -> list[str]:
        errors: list[str] = []
        if self.schema_version != 1:
            errors.append("schema_version no soportada")
        if not self.node_id or not self.mqtt_namespace or not self.mqtt_broker:
            errors.append("node_id, mqtt_namespace y mqtt_broker son obligatorios")
        if any(char.isspace() for char in self.mqtt_broker) or "#" in self.mqtt_broker:
            errors.append("mqtt_broker no puede contener espacios ni comentarios")
        if not 1 <= int(self.mqtt_port) <= 65535:
            errors.append("mqtt_port inválido")
        if not 1 <= int(self.mqtt_keepalive) <= 3600:
            errors.append("mqtt_keepalive inválido")
        if not 1 <= self.mqtt_reconnect_min_seconds <= self.mqtt_reconnect_max_seconds <= 3600:
            errors.append("backoff MQTT inválido")
        if self.protocol not in {"raw_udp", "rtp", "tcp_server"}:
            errors.append("protocol inválido")
        if self.sample_rate not in {8000, 16000, 32000, 44100, 48000, 96000}:
            errors.append("sample_rate inválido")
        if self.channels not in {1, 2} or self.bit_depth not in {16, 24}:
            errors.append("channels o bit_depth inválidos")
        if not -20.0 <= float(self.gain_db) <= 20.0:
            errors.append("gain_db fuera de rango")
        if not 1000 <= int(self.alsa_buffer_time_us) <= 500000:
            errors.append("alsa_buffer_time_us fuera de rango")
        if not 1 <= int(self.pipeline_queue_ms) <= 1000:
            errors.append("pipeline_queue_ms fuera de rango")
        try:
            self.normalize_rode_mode(self.rode_mode)
        except ValueError as exc:
            errors.append(str(exc))
        if self.protocol == "tcp_server":
            if not _is_ip(self.stream_bind_ip) or not 1 <= int(self.stream_port) <= 65535:
                errors.append("stream_bind_ip o stream_port inválidos")
        elif not (allow_missing_target and not self.dest_ip and self.dest_port == 0):
            if not _is_ip(self.dest_ip) or not 1 <= int(self.dest_port) <= 65535:
                errors.append("dest_ip o dest_port inválidos")
        return errors


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False
