"""
device_discovery.py — Descubrimiento dinámico de dispositivos de audio.

Identifica dispositivos ALSA por nombre en lugar de por índice de tarjeta
(hw:2,0 es frágil — el índice cambia con cada boot/reconexión USB).
"""

import re
import subprocess
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  Descubrimiento ALSA
# ─────────────────────────────────────────────────────────────────────────────

def find_alsa_device_by_name(name_substring: str) -> Optional[str]:
    """
    Busca un dispositivo de captura ALSA por nombre parcial.

    Parsea la salida de `arecord -l` buscando una línea que contenga
    name_substring. Devuelve la cadena 'hw:X,Y' correspondiente.

    Ejemplo:
        >>> find_alsa_device_by_name("AI-Micro")
        'hw:2,0'
        >>> find_alsa_device_by_name("AI-Micro")  # no conectado
        None
    """
    try:
        result = subprocess.run(
            ["arecord", "-l"],
            capture_output=True, text=True, timeout=5
        )
        output = result.stdout + result.stderr
    except FileNotFoundError:
        logger.error("arecord no encontrado. ¿Está instalado alsa-utils?")
        return None
    except subprocess.TimeoutExpired:
        logger.error("Timeout ejecutando arecord -l")
        return None

    # Parsear líneas del estilo:
    #   card 2: Micro [RØDE AI-Micro], device 0: USB Audio [USB Audio]
    pattern = re.compile(
        r"card\s+(\d+):.*?" + re.escape(name_substring) + r".*?device\s+(\d+):",
        re.IGNORECASE
    )
    for line in output.splitlines():
        m = pattern.search(line)
        if m:
            card, device = m.group(1), m.group(2)
            alsa_dev = f"hw:{card},{device}"
            logger.info(f"Dispositivo '{name_substring}' encontrado: {alsa_dev}")
            return alsa_dev

    # Segunda pasada: buscar líneas que contengan el nombre aunque no coincida exactamente
    name_lower = name_substring.lower()
    for line in output.splitlines():
        if name_lower in line.lower():
            m = re.search(r"card\s+(\d+).*?device\s+(\d+):", line, re.IGNORECASE)
            if m:
                card, device = m.group(1), m.group(2)
                alsa_dev = f"hw:{card},{device}"
                logger.info(f"Dispositivo '{name_substring}' encontrado (fuzzy): {alsa_dev}")
                return alsa_dev

    logger.warning(f"Dispositivo '{name_substring}' no encontrado en arecord -l")
    return None


def find_alsa_playback_device_by_name(name_substring: str) -> Optional[str]:
    """
    Igual que find_alsa_device_by_name pero para reproducción (aplay -l).
    """
    try:
        result = subprocess.run(
            ["aplay", "-l"],
            capture_output=True, text=True, timeout=5
        )
        output = result.stdout + result.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.error(f"Error ejecutando aplay -l: {e}")
        return None

    pattern = re.compile(
        r"card\s+(\d+):.*?" + re.escape(name_substring) + r".*?device\s+(\d+):",
        re.IGNORECASE
    )
    for line in output.splitlines():
        m = pattern.search(line)
        if not m and name_substring.lower() in line.lower():
            m = re.search(r"card\s+(\d+).*?device\s+(\d+):", line, re.IGNORECASE)
        if m:
            card, device = m.group(1), m.group(2)
            alsa_dev = f"hw:{card},{device}"
            logger.info(f"Playback '{name_substring}': {alsa_dev}")
            return alsa_dev

    logger.warning(f"Playback '{name_substring}' no encontrado en aplay -l")
    return None


def list_alsa_capture_devices() -> list[dict]:
    """
    Devuelve lista de todos los dispositivos de captura disponibles.

    Returns:
        Lista de dicts con claves: card, device, name, alsa_id
    """
    devices = []
    try:
        result = subprocess.run(
            ["arecord", "-l"],
            capture_output=True, text=True, timeout=5
        )
        for line in (result.stdout + result.stderr).splitlines():
            # Parsea líneas como:
            #   card 2: Micro [RØDE AI-Micro], device 0: USB Audio [USB Audio]
            # El nombre antes de [] puede tener espacios, por eso .+? en lugar de \S+
            m = re.search(r"card\s+(\d+):\s+\S+\s+\[(.+?)\].*?device\s+(\d+):\s+.+?\[(.+?)\]", line)
            if m:
                card, card_name, device, dev_name = m.groups()
                devices.append({
                    "card": int(card),
                    "device": int(device),
                    "card_name": card_name,
                    "device_name": dev_name,
                    "alsa_id": f"hw:{card},{device}",
                })
    except Exception as e:
        logger.debug(f"list_alsa_capture_devices error: {e}")
    return devices


def validate_alsa_device(alsa_id: str, sample_rate: int = 48000,
                          channels: int = 2) -> bool:
    """
    Comprueba si un dispositivo ALSA puede abrirse con los parámetros dados.
    Usa arecord con -d 0 (duración 0 = solo abre y cierra).
    """
    try:
        result = subprocess.run(
            [
                "arecord",
                "-D", alsa_id,
                "--dump-hw-params",
                "-d", "1",
                "-q",
            ],
            capture_output=True, text=True, timeout=4
        )
        # Si devuelve 0 o el stderr contiene info de HW params, el dispositivo existe
        return result.returncode == 0 or "HW Params" in result.stderr
    except subprocess.TimeoutExpired:
        return False
    except FileNotFoundError:
        return False


# ─────────────────────────────────────────────────────────────────────────────
#  Descubrimiento por sounddevice (alternativa sin arecord)
# ─────────────────────────────────────────────────────────────────────────────

def find_sounddevice_by_name(name_substring: str,
                              require_inputs: int = 1) -> Optional[int]:
    """
    Busca un dispositivo de audio por nombre usando la librería sounddevice.
    Devuelve el índice del dispositivo o None.

    Útil como alternativa a find_alsa_device_by_name cuando sounddevice
    está disponible.
    """
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        name_lower = name_substring.lower()
        for i, dev in enumerate(devices):
            if name_lower in dev["name"].lower():
                if dev["max_input_channels"] >= require_inputs:
                    logger.info(f"sounddevice: '{dev['name']}' (idx={i}, inputs={dev['max_input_channels']})")
                    return i
                else:
                    logger.debug(f"sounddevice: '{dev['name']}' tiene solo {dev['max_input_channels']} input(s)")
    except ImportError:
        logger.debug("sounddevice no disponible")
    except Exception as e:
        logger.debug(f"find_sounddevice_by_name error: {e}")
    return None


# ─────────────────────────────────────────────────────────────────────────────
#  Utilidades de diagnóstico
# ─────────────────────────────────────────────────────────────────────────────

def get_supported_sample_rates(alsa_id: str,
                                candidates: list[int] = None) -> list[int]:
    """
    Comprueba qué sample rates soporta un dispositivo ALSA dado.
    """
    if candidates is None:
        candidates = [48000, 44100, 32000, 16000, 8000]

    supported = []
    for rate in candidates:
        try:
            result = subprocess.run(
                ["arecord", "-D", alsa_id, "-r", str(rate), "--dump-hw-params", "-d", "0"],
                capture_output=True, text=True, timeout=3
            )
            if result.returncode == 0:
                supported.append(rate)
        except Exception:
            pass
    return supported


def print_audio_devices_summary() -> None:
    """Imprime un resumen de dispositivos de audio disponibles (diagnóstico)."""
    print("=== Dispositivos de captura (arecord -l) ===")
    try:
        r = subprocess.run(["arecord", "-l"], capture_output=True, text=True, timeout=5)
        print(r.stdout or "(vacío)")
    except Exception as e:
        print(f"Error: {e}")
    print("\n=== Dispositivos de reproduccion (aplay -l) ===")
    try:
        r = subprocess.run(["aplay", "-l"], capture_output=True, text=True, timeout=5)
        print(r.stdout or "(vacio)")
    except Exception as e:
        print(f"Error: {e}")
