#!/usr/bin/env python3
"""Migra la configuración anterior sin mostrar credenciales por pantalla."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio_capture_service.config import AudioCaptureConfig


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy-json", default="/etc/nexor/audio_capture.json")
    parser.add_argument("--legacy-env", default="/etc/nexor/audio_capture.env")
    parser.add_argument("--output", default="/etc/nexor/audio-binaural.json")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    output = Path(args.output)
    if output.exists() and not args.force:
        raise SystemExit(f"Ya existe {output}; usa --force para reemplazarlo.")

    legacy_path = Path(args.legacy_json)
    legacy = json.loads(legacy_path.read_text(encoding="utf-8")) if legacy_path.exists() else {}
    cfg = AudioCaptureConfig.from_dict(legacy)
    env = read_env(Path(args.legacy_env))
    values = cfg.to_dict()
    for env_key, field, cast in (
        ("MQTT_BROKER", "mqtt_broker", str),
        ("MQTT_PORT", "mqtt_port", int),
        ("MQTT_USER", "mqtt_user", str),
        ("MQTT_PASSWORD", "mqtt_password", str),
        ("NODE_ID", "node_id", str),
    ):
        if env_key in env:
            values[field] = cast(env[env_key])

    cfg = AudioCaptureConfig.from_dict(values)
    cfg = replace(
        cfg,
        alsa_device_override=None,
        dest_ip="",
        dest_port=0,
        wait_for_mqtt_target=True,
        alsa_buffer_time_us=max(1000, cfg.alsa_buffer_time_us),
        pipeline_queue_ms=max(1, cfg.pipeline_queue_ms),
    )
    errors = cfg.validate(allow_missing_target=True)
    if errors:
        raise SystemExit("La configuración migrada no es válida: " + "; ".join(errors))

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp")
    temporary.write_text(json.dumps(cfg.to_dict(), indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o640)
    os.replace(temporary, output)
    print(f"Configuración migrada a {output}. El destino de audio debe llegar por MQTT.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
