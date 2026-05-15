"""
RodeController — Control del micrófono RODE AI-Micro vía USB HID.

Consolida la funcionalidad de los scripts experimentales:
  - consultar_modo_rode_6.py  →  get_mode()
  - rode_mode_switch_3.py     →  set_mode()
  - ResetUSBInterfaces.py     →  reset_usb()
  - RodeMicroStatus.py        →  is_available() (sin matar PulseAudio)

Uso:
    controller = RodeController()
    if controller.is_available():
        mode = controller.get_mode()          # "STEREO" | "SPLIT" | "MERGED" | None
        controller.set_mode("STEREO")
"""

import time
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
#  Constantes del dispositivo
# ─────────────────────────────────────────────────────────────────────────────
VENDOR_ID  = 0x19f7
PRODUCT_ID = 0x0023
HID_IF     = 0       # Interfaz HID (control)
AUDIO_IF   = 1       # Interfaz de audio

MODES = {
    "MERGED": 0x00,
    "SPLIT":  0x01,
    "STEREO": 0x02,
}
MODES_INV = {v: k for k, v in MODES.items()}

# ─────────────────────────────────────────────────────────────────────────────
#  SET_REPORT payloads (28 bytes cada uno, primer byte = ReportID 0x0A)
# ─────────────────────────────────────────────────────────────────────────────
_SR_CONSULTA = bytes([
    0x0A, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00,
])

_SR_FINAL = bytes([
    0x0A, 0x04, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00,
])

# Prefijos de los 7 SET_REPORTs iniciales del cambio de modo
_SR_SWITCH_PREFIXES = [
    bytes.fromhex("0a010100000000000000000000000000000000000000000000000000"),
    bytes.fromhex("0a020100000000000000000000000000000000000000000000000000"),
    bytes.fromhex("0a050100000000000000000000000000000000000000000000000000"),
    bytes.fromhex("0a060100000000000000000000000000000000000000000000000000"),
    bytes.fromhex("0a030100000000000000000000000000000000000000000000000000"),
    bytes.fromhex("0a040100000000000000000000000000000000000000000000000000"),
    bytes.fromhex("0a000000000000000000000000000000000000000000000000000000"),
]


class RodeControllerError(RuntimeError):
    """Error de comunicación con el RODE AI-Micro."""


class RodeController:
    """
    Controlador del micrófono RODE AI-Micro.

    Puede usarse como context manager para garantizar el cleanup USB:

        with RodeController() as rode:
            mode = rode.get_mode()
            rode.set_mode("STEREO")

    O de forma directa (el cleanup se hace explícitamente):

        rode = RodeController()
        if rode.is_available():
            rode.set_mode("SPLIT")
    """

    def __init__(self) -> None:
        self._dev = None  # usb.core.Device activo (si hay operación en curso)

    # ─────────────────────────────────────────────────────────────────────────
    #  API pública
    # ─────────────────────────────────────────────────────────────────────────

    def is_available(self) -> bool:
        """
        Comprueba si el RODE AI-Micro está conectado y reconocido por el SO.
        NO mata PulseAudio ni accede al dispositivo de audio.
        """
        try:
            import usb.core
            dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
            return dev is not None
        except Exception as e:
            logger.debug(f"is_available(): excepción USB: {e}")
            return False

    def get_mode(self) -> Optional[str]:
        """
        Consulta el modo actual del RODE AI-Micro.

        Secuencia USB (descubierta experimentalmente en consultar_modo_rode_6.py):
          1. Claim HID
          2. Handshake GET_DESCRIPTOR
          3. SET_REPORT_final → drain InterruptIN
          4. SET_REPORT_consulta → leer InterruptIN e interpretar byte[3]
          5. SET_REPORT_final → drain InterruptIN
          6. Reattach AUDIO kernel driver

        Returns:
            "STEREO" | "SPLIT" | "MERGED" | None  (None = no detectado)
        """
        import usb.core
        import usb.util

        dev = self._open_device()
        if dev is None:
            return None

        try:
            self._detach_kernel(dev, HID_IF)
            usb.util.claim_interface(dev, HID_IF)

            # Handshake
            for idx in [1, 2]:
                self._get_string_descriptor(dev, idx)

            # SET_REPORT final inicial → drain
            self._send_set_report(dev, _SR_FINAL)
            self._drain_interrupt_in(dev)

            # SET_REPORT consulta → leer modo
            self._send_set_report(dev, _SR_CONSULTA)
            mode_byte = self._read_mode_from_interrupt(dev)

            # SET_REPORT final de cierre → drain
            self._send_set_report(dev, _SR_FINAL)
            self._drain_interrupt_in(dev)

            mode = MODES_INV.get(mode_byte)
            logger.info(f"RODE mode: {mode} (byte=0x{mode_byte:02x})" if mode_byte is not None else "RODE mode: unknown")
            return mode

        except Exception as e:
            logger.error(f"Error leyendo modo RODE: {e}")
            raise RodeControllerError(f"get_mode failed: {e}") from e
        finally:
            self._release_and_reattach(dev, HID_IF, reattach_audio=True)

    def set_mode(self, mode: str) -> bool:
        """
        Cambia el modo del RODE AI-Micro.

        Secuencia USB (rode_mode_switch_3.py):
          1. Detach HID + AUDIO kernel drivers
          2. Claim HID
          3. SET_IDLE
          4. Enviar 8 SET_REPORTs (último lleva el byte de modo)
          5. Release HID, reset dispositivo
          6. Reattach AUDIO kernel driver

        Args:
            mode: "STEREO" | "SPLIT" | "MERGED"

        Returns:
            True si el cambio se completó sin errores.
        """
        import usb.core
        import usb.util

        mode = mode.upper()
        if mode not in MODES:
            raise ValueError(f"Modo inválido '{mode}'. Válidos: {list(MODES.keys())}")

        mode_byte = MODES[mode]
        logger.info(f"Estableciendo modo RODE: {mode} (byte=0x{mode_byte:02x})")

        dev = self._open_device()
        if dev is None:
            return False

        try:
            # Detach ambos drivers
            self._detach_kernel(dev, HID_IF)
            self._detach_kernel(dev, AUDIO_IF)

            usb.util.claim_interface(dev, HID_IF)

            # Configurar + SET_IDLE
            try:
                dev.set_configuration()
            except usb.core.USBError:
                pass  # Ya configurado

            try:
                dev.ctrl_transfer(0x21, 0x0A, 0x0000, HID_IF, None, timeout=500)
            except usb.core.USBError as e:
                logger.debug(f"SET_IDLE error (no crítico): {e}")

            # Enviar los 7 SET_REPORTs prefijos
            for i, payload in enumerate(_SR_SWITCH_PREFIXES):
                self._send_set_report(dev, payload, wvalue=0x020A)
                time.sleep(0.05)

            # SET_REPORT #8 con el modo elegido
            mode_payload = bytes([
                0x0A, 0x00, 0x00, mode_byte,
                *([0x00] * 24)
            ])
            self._send_set_report(dev, mode_payload, wvalue=0x020A)
            time.sleep(0.05)

            # Release + reset
            usb.util.release_interface(dev, HID_IF)
            usb.util.dispose_resources(dev)

            try:
                dev.reset()
                logger.debug("USB reset enviado")
            except usb.core.USBError as e:
                logger.warning(f"USB reset error (puede ser normal): {e}")

            time.sleep(0.6)  # Dar tiempo al dispositivo a re-enumerar

            # Reattach del driver de audio
            dev_post = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
            if dev_post:
                try:
                    dev_post.attach_kernel_driver(AUDIO_IF)
                    logger.debug("Driver de audio reattached")
                except usb.core.USBError as e:
                    logger.debug(f"Reattach audio driver (puede ser normal si ya estaba): {e}")
            else:
                logger.warning("Dispositivo no encontrado tras reset (re-enumerando...)")

            logger.info(f"Modo {mode} establecido correctamente")
            return True

        except Exception as e:
            logger.error(f"Error estableciendo modo RODE {mode}: {e}")
            raise RodeControllerError(f"set_mode({mode}) failed: {e}") from e

    def reset_usb(self) -> bool:
        """
        Reinicia el dispositivo USB del RODE AI-Micro vía sysfs (unbind/bind).
        Requiere privilegios de root.

        Returns:
            True si el reset se completó.
        """
        sysfs_name = self._find_sysfs_device()
        if sysfs_name is None:
            logger.warning("Dispositivo RODE no encontrado en sysfs para reset")
            return False

        try:
            logger.info(f"Reiniciando RODE via sysfs: {sysfs_name}")
            unbind = "/sys/bus/usb/drivers/usb/unbind"
            bind   = "/sys/bus/usb/drivers/usb/bind"

            with open(unbind, "w") as f:
                f.write(sysfs_name)
            logger.debug(f"Unbind OK: {sysfs_name}")
            time.sleep(1.0)

            with open(bind, "w") as f:
                f.write(sysfs_name)
            logger.debug(f"Bind OK: {sysfs_name}")
            time.sleep(1.5)  # Tiempo para re-enumerar + ALSA

            logger.info("Reset USB completado")
            return True

        except PermissionError:
            logger.error("reset_usb() requiere permisos de root")
            return False
        except OSError as e:
            logger.error(f"reset_usb() error sysfs: {e}")
            return False

    # ─────────────────────────────────────────────────────────────────────────
    #  Context manager
    # ─────────────────────────────────────────────────────────────────────────

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # El cleanup USB se hace dentro de cada operación (get_mode/set_mode)
        # Este context manager existe para uso semántico y posibles futuras extensiones
        return False

    # ─────────────────────────────────────────────────────────────────────────
    #  Métodos privados de comunicación USB
    # ─────────────────────────────────────────────────────────────────────────

    def _open_device(self):
        """Localiza y devuelve el dispositivo USB. None si no está conectado."""
        try:
            import usb.core
            dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
            if dev is None:
                logger.warning("RODE AI-Micro no encontrado (¿desconectado?)")
            return dev
        except Exception as e:
            logger.error(f"Error buscando dispositivo USB: {e}")
            return None

    def _detach_kernel(self, dev, interface: int) -> None:
        """Detach del kernel driver si está activo. Silencia errores."""
        try:
            if dev.is_kernel_driver_active(interface):
                dev.detach_kernel_driver(interface)
                logger.debug(f"Kernel driver detached de interfaz {interface}")
        except Exception as e:
            logger.debug(f"detach_kernel({interface}): {e}")

    def _get_string_descriptor(self, dev, index: int) -> bytes:
        """Envía GET_DESCRIPTOR STRING (handshake requerido por el RODE)."""
        try:
            return bytes(dev.ctrl_transfer(
                0x80, 0x06, (0x03 << 8) | index, 0x0409, 255, timeout=1000
            ))
        except Exception as e:
            logger.debug(f"GET_DESCRIPTOR({index}): {e}")
            return b""

    def _send_set_report(self, dev, payload: bytes, wvalue: int = 0x030A) -> None:
        """Envía un SET_REPORT a la interfaz HID."""
        import usb.core
        try:
            dev.ctrl_transfer(0x21, 0x09, wvalue, HID_IF, payload, timeout=1000)
        except usb.core.USBError as e:
            logger.debug(f"SET_REPORT error: {e}")

    def _drain_interrupt_in(self, dev, timeout_ms: int = 1500) -> None:
        """Lee el endpoint InterruptIN hasta timeout (limpia el buffer)."""
        import usb.core
        try:
            while True:
                dev.read(0x81, 64, timeout=timeout_ms)
                time.sleep(0.05)
        except usb.core.USBError as e:
            if e.errno == 110:  # ETIMEDOUT — fin del stream, esperado
                pass
            else:
                logger.debug(f"drain_interrupt_in error: {e}")

    def _read_mode_from_interrupt(self, dev, timeout_ms: int = 2000) -> Optional[int]:
        """
        Lee el endpoint InterruptIN y extrae el byte de modo (byte[3]).
        Devuelve el entero del modo o None.
        """
        import usb.core
        mode_byte = None
        try:
            while True:
                resp = dev.read(0x81, 64, timeout=timeout_ms)
                if len(resp) >= 4:
                    mode_byte = resp[3]
                    logger.debug(f"InterruptIN: {' '.join(f'{b:02x}' for b in resp[:8])}")
                time.sleep(0.05)
        except usb.core.USBError as e:
            if e.errno != 110:  # 110 = timeout, es lo esperado
                logger.debug(f"read_mode_from_interrupt error: {e}")
        return mode_byte

    def _release_and_reattach(self, dev, hid_if: int, reattach_audio: bool = True) -> None:
        """Cleanup: release interfaz HID y reattach del driver de audio."""
        import usb.core
        import usb.util
        try:
            usb.util.release_interface(dev, hid_if)
        except Exception as e:
            logger.debug(f"release_interface({hid_if}): {e}")

        if reattach_audio:
            try:
                dev.attach_kernel_driver(AUDIO_IF)
                logger.debug("Driver de audio reattached")
            except usb.core.USBError as e:
                logger.debug(f"attach_kernel_driver(AUDIO_IF): {e}")

    def _find_sysfs_device(self) -> Optional[str]:
        """Busca el dispositivo RODE en /sys/bus/usb/devices/ por VID/PID."""
        vid_str = f"{VENDOR_ID:04x}"
        pid_str = f"{PRODUCT_ID:04x}"
        usb_path = "/sys/bus/usb/devices"
        try:
            for dev_name in os.listdir(usb_path):
                dev_path = os.path.join(usb_path, dev_name)
                try:
                    with open(os.path.join(dev_path, "idVendor")) as f:
                        vid = f.read().strip().lower()
                    with open(os.path.join(dev_path, "idProduct")) as f:
                        pid = f.read().strip().lower()
                    if vid == vid_str and pid == pid_str:
                        return dev_name
                except FileNotFoundError:
                    continue
        except OSError as e:
            logger.warning(f"No se pudo leer /sys/bus/usb/devices: {e}")
        return None
