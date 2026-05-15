#!/usr/bin/env python3
"""
test_service.py — Tests del Audio Capture Service (sin hardware)

Ejecutar desde StreamAudio/Linux/:
    python3 test_service.py

Prueba:
  1. Sintaxis Python de todos los módulos
  2. Importación de módulos
  3. Config load/save/apply_delta/validate
  4. Device discovery (parseo de arecord -l)
  5. Pipeline string builder (sin GStreamer real)
  6. MQTT adapter (sin broker real)
  7. Modo simulación del servicio completo
"""

import json
import os
import sys
import tempfile
import unittest
import subprocess

# Añadir el directorio padre al path para importar el paquete
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ─────────────────────────────────────────────────────────────────────────────

class TestSyntax(unittest.TestCase):
    """Verifica que todos los ficheros Python tengan sintaxis correcta."""

    MODULE_FILES = [
        "audio_capture_service/__init__.py",
        "audio_capture_service/config.py",
        "audio_capture_service/device_discovery.py",
        "audio_capture_service/rode_controller.py",
        "audio_capture_service/pipeline.py",
        "audio_capture_service/mqtt_adapter.py",
        "audio_capture_service/main.py",
    ]

    def test_syntax_all_modules(self):
        base = os.path.dirname(os.path.abspath(__file__))
        for rel_path in self.MODULE_FILES:
            full_path = os.path.join(base, rel_path)
            with self.subTest(file=rel_path):
                result = subprocess.run(
                    [sys.executable, "-m", "py_compile", full_path],
                    capture_output=True, text=True
                )
                self.assertEqual(
                    result.returncode, 0,
                    f"Syntax error en {rel_path}:\n{result.stderr}"
                )
                print(f"  ✓ Sintaxis OK: {rel_path}")


# ─────────────────────────────────────────────────────────────────────────────

class TestConfig(unittest.TestCase):
    """Tests de configuración — no requiere hardware."""

    def setUp(self):
        from audio_capture_service.config import AudioCaptureConfig
        self.Config = AudioCaptureConfig

    def test_default_config(self):
        cfg = self.Config()
        self.assertEqual(cfg.sample_rate, 48000)
        self.assertEqual(cfg.channels, 2)
        self.assertEqual(cfg.bit_depth, 24)
        self.assertFalse(cfg.muted)
        self.assertEqual(cfg.rode_mode, "SPLIT")
        self.assertEqual(cfg.protocol, "raw_udp")
        print(f"  ✓ Config por defecto correcta")

    def test_validate_ok(self):
        cfg = self.Config()
        errors = cfg.validate()
        self.assertEqual(errors, [], f"Errores de validación inesperados: {errors}")
        print(f"  ✓ Validación correcta")

    def test_validate_bad_bit_depth(self):
        cfg = self.Config(bit_depth=32)
        errors = cfg.validate()
        self.assertTrue(any("bit_depth" in e for e in errors))
        print(f"  ✓ Validación detecta bit_depth inválido")

    def test_validate_bad_port(self):
        cfg = self.Config(protocol="raw_udp", dest_port=99999)
        errors = cfg.validate()
        self.assertTrue(any("dest_port" in e for e in errors))
        print(f"  ✓ Validación detecta dest_port inválido")

    def test_apply_delta(self):
        cfg = self.Config()
        new_cfg = cfg.apply_delta({"gain_db": 3.0, "muted": True, "sample_rate": 44100})
        self.assertEqual(new_cfg.gain_db, 3.0)
        self.assertTrue(new_cfg.muted)
        self.assertEqual(new_cfg.sample_rate, 44100)
        # El original no cambia
        self.assertEqual(cfg.gain_db, 0.0)
        print(f"  ✓ apply_delta funciona correctamente")

    def test_apply_delta_ignores_unknown_keys(self):
        cfg = self.Config()
        new_cfg = cfg.apply_delta({"clave_desconocida": 999, "gain_db": 1.0})
        self.assertEqual(new_cfg.gain_db, 1.0)
        self.assertFalse(hasattr(new_cfg, "clave_desconocida"))
        print(f"  ✓ apply_delta ignora claves desconocidas")

    def test_save_and_load(self):
        cfg = self.Config(
            dest_ip="10.0.0.5",
            dest_port=1234,
            gain_db=6.0,
            node_id="nexor-test"
        )
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            tmp_path = f.name
        try:
            cfg.save(tmp_path)
            loaded = self.Config.load(tmp_path)
            self.assertEqual(loaded.dest_ip, "10.0.0.5")
            self.assertEqual(loaded.dest_port, 1234)
            self.assertAlmostEqual(loaded.gain_db, 6.0)
            self.assertEqual(loaded.node_id, "nexor-test")
            print(f"  ✓ save/load JSON funciona correctamente")
        finally:
            os.unlink(tmp_path)

    def test_load_missing_file(self):
        cfg = self.Config.load("/tmp/no_existe_este_fichero_12345.json")
        self.assertIsInstance(cfg, self.Config)
        print(f"  ✓ load() con fichero inexistente devuelve config por defecto")

    def test_load_corrupt_json(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            f.write("{esto no es json válido")
            tmp_path = f.name
        try:
            cfg = self.Config.load(tmp_path)
            self.assertIsInstance(cfg, self.Config)
            print(f"  ✓ load() con JSON corrupto devuelve config por defecto")
        finally:
            os.unlink(tmp_path)

    def test_derived_properties(self):
        cfg = self.Config(bit_depth=24, gain_db=0.0)
        self.assertEqual(cfg.gst_format, "S24LE")
        self.assertEqual(cfg.rtp_payloader, "rtpL24pay")
        self.assertAlmostEqual(cfg.gain_linear, 1.0, places=4)

        cfg16 = self.Config(bit_depth=16)
        self.assertEqual(cfg16.gst_format, "S16LE")
        self.assertEqual(cfg16.rtp_payloader, "rtpL16pay")

        cfg_3db = self.Config(gain_db=20.0)
        self.assertAlmostEqual(cfg_3db.gain_linear, 10.0, places=2)
        print(f"  ✓ Propiedades derivadas correctas")

    def test_mqtt_topics(self):
        cfg = self.Config(node_id="nexor-42")
        self.assertEqual(cfg.mqtt_cmd_topic,    "nexor/v1/nodes/nexor-42/services/audio_binaural/cmd")
        self.assertEqual(cfg.mqtt_state_topic,  "nexor/v1/nodes/nexor-42/services/audio_binaural/state")
        self.assertEqual(cfg.mqtt_config_desired_topic, "nexor/v1/nodes/nexor-42/services/audio_binaural/config/desired")
        print(f"  ✓ Topics MQTT generados correctamente")

    def test_env_overrides(self):
        os.environ["AUDIO_DEST_PORT"] = "9999"
        try:
            with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
                json.dump({}, f)
                tmp_path = f.name
            cfg = self.Config.load_with_env_overrides(tmp_path)
            self.assertEqual(cfg.dest_port, 9999)
            print(f"  ✓ Variables de entorno sobreescriben config correctamente")
        finally:
            os.environ.pop("AUDIO_DEST_PORT", None)
            os.unlink(tmp_path)


# ─────────────────────────────────────────────────────────────────────────────

class TestDeviceDiscovery(unittest.TestCase):
    """Tests de device discovery — con salida simulada de arecord."""

    def test_parse_alsa_output_with_rode(self):
        """Verifica que el parser extrae el dispositivo correcto de arecord -l."""
        from audio_capture_service import device_discovery as dd
        import unittest.mock as mock

        fake_arecord_output = """\
**** List of CAPTURE Hardware Devices ****
card 0: Generic [HD-Audio Generic], device 0: ALC897 Analog [ALC897 Analog]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
card 2: Micro [RØDE AI-Micro], device 0: USB Audio [USB Audio]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
"""
        mock_result = mock.Mock()
        mock_result.stdout = fake_arecord_output
        mock_result.stderr = ""

        with mock.patch("subprocess.run", return_value=mock_result):
            result = dd.find_alsa_device_by_name("AI-Micro")

        self.assertEqual(result, "hw:2,0")
        print(f"  ✓ Parser ALSA extrae 'hw:2,0' correctamente")

    def test_parse_alsa_output_no_device(self):
        """Verifica que devuelve None cuando el dispositivo no está."""
        from audio_capture_service import device_discovery as dd
        import unittest.mock as mock

        fake_output = "card 0: Generic [HD-Audio Generic], device 0: ALC897 Analog [ALC897]\n"
        mock_result = mock.Mock()
        mock_result.stdout = fake_output
        mock_result.stderr = ""

        with mock.patch("subprocess.run", return_value=mock_result):
            result = dd.find_alsa_device_by_name("AI-Micro")

        self.assertIsNone(result)
        print(f"  ✓ Parser ALSA devuelve None cuando no encuentra el dispositivo")

    def test_list_devices(self):
        """Verifica que list_alsa_capture_devices parsea correctamente."""
        from audio_capture_service import device_discovery as dd
        import unittest.mock as mock

        fake_output = """\
card 0: Generic [HD-Audio Generic], device 0: ALC897 Analog [ALC897 Analog]
card 2: Micro [RØDE AI-Micro], device 0: USB Audio [USB Audio]
"""
        mock_result = mock.Mock()
        mock_result.stdout = fake_output
        mock_result.stderr = ""

        with mock.patch("subprocess.run", return_value=mock_result):
            devices = dd.list_alsa_capture_devices()

        self.assertGreaterEqual(len(devices), 1)
        alsa_ids = [d["alsa_id"] for d in devices]
        self.assertIn("hw:0,0", alsa_ids)
        print(f"  ✓ list_alsa_capture_devices parsea {len(devices)} dispositivos")


# ─────────────────────────────────────────────────────────────────────────────

class TestPipelineString(unittest.TestCase):
    """Tests del pipeline builder — sin GStreamer real."""

    def _make_cfg(self, **kwargs):
        from audio_capture_service.config import AudioCaptureConfig
        defaults = dict(
            dest_ip="192.168.0.20",
            dest_port=5004,
            protocol="raw_udp",
            sample_rate=48000,
            channels=2,
            bit_depth=24,
            gain_db=0.0,
            muted=False,
            alsa_device_override="hw:2,0",
            pipeline_queue_ms=100,
            alsa_buffer_time_us=50000,
            node_id="test",
        )
        defaults.update(kwargs)
        return AudioCaptureConfig(**defaults)

    def test_raw_udp_pipeline_contains_no_wavenc(self):
        from audio_capture_service.pipeline import AudioPipeline
        pipeline = AudioPipeline()
        cfg = self._make_cfg(protocol="raw_udp")
        s = pipeline._build_pipeline_string(cfg)
        self.assertNotIn("wavenc", s, "wavenc no debe estar en el pipeline raw_udp")
        self.assertIn("udpsink", s)
        self.assertIn("hw:2,0", s)
        self.assertIn("S24LE", s)
        self.assertIn("48000", s)
        print(f"  ✓ Pipeline raw_udp sin wavenc: OK")

    def test_rtp_pipeline_has_payloader(self):
        from audio_capture_service.pipeline import AudioPipeline
        pipeline = AudioPipeline()
        cfg = self._make_cfg(protocol="rtp", bit_depth=24)
        s = pipeline._build_pipeline_string(cfg)
        self.assertIn("rtpL24pay", s)
        self.assertNotIn("wavenc", s)
        print(f"  ✓ Pipeline RTP tiene rtpL24pay: OK")

    def test_rtp_16bit_pipeline(self):
        from audio_capture_service.pipeline import AudioPipeline
        pipeline = AudioPipeline()
        cfg = self._make_cfg(protocol="rtp", bit_depth=16)
        s = pipeline._build_pipeline_string(cfg)
        self.assertIn("rtpL16pay", s)
        self.assertIn("S16LE", s)
        print(f"  ✓ Pipeline RTP 16-bit: OK")

    def test_pipeline_has_volume_element(self):
        from audio_capture_service.pipeline import AudioPipeline
        pipeline = AudioPipeline()
        cfg = self._make_cfg()
        s = pipeline._build_pipeline_string(cfg)
        self.assertIn("volume name=vol", s)
        print(f"  ✓ Pipeline tiene elemento volume: OK")

    def test_pipeline_has_queue_with_time(self):
        from audio_capture_service.pipeline import AudioPipeline
        pipeline = AudioPipeline()
        cfg = self._make_cfg(pipeline_queue_ms=100)
        s = pipeline._build_pipeline_string(cfg)
        self.assertIn("max-size-time=100000000", s)  # 100ms en ns
        self.assertIn("max-size-bytes=0", s)
        self.assertIn("max-size-buffers=0", s)
        print(f"  ✓ Pipeline tiene queue con límite por tiempo: OK")

    def test_muted_pipeline(self):
        from audio_capture_service.pipeline import AudioPipeline
        pipeline = AudioPipeline()
        cfg = self._make_cfg(muted=True)
        s = pipeline._build_pipeline_string(cfg)
        self.assertIn("mute=true", s)
        print(f"  ✓ Pipeline en mute: OK")

    def test_dest_ip_in_pipeline(self):
        from audio_capture_service.pipeline import AudioPipeline
        pipeline = AudioPipeline()
        cfg = self._make_cfg(dest_ip="10.0.0.5", dest_port=9999)
        s = pipeline._build_pipeline_string(cfg)
        self.assertIn("host=10.0.0.5", s)
        self.assertIn("port=9999", s)
        print(f"  ✓ Pipeline tiene IP y puerto correctos: OK")


# ─────────────────────────────────────────────────────────────────────────────

class TestMqttAdapter(unittest.TestCase):
    """Tests del MQTT adapter — sin broker real."""

    def _make_cfg(self):
        from audio_capture_service.config import AudioCaptureConfig
        return AudioCaptureConfig(node_id="test-01")

    def test_instantiation(self):
        from audio_capture_service.mqtt_adapter import AudioCaptureServiceAdapter
        cfg = self._make_cfg()
        adapter = AudioCaptureServiceAdapter(cfg)
        self.assertIsNotNone(adapter)
        self.assertFalse(adapter.is_connected())
        print(f"  ✓ AudioCaptureServiceAdapter instanciado correctamente")

    def test_command_dispatch(self):
        """Verifica que los comandos MQTT disparan los callbacks correctos."""
        from audio_capture_service.mqtt_adapter import AudioCaptureServiceAdapter
        cfg = self._make_cfg()

        called = {}
        adapter = AudioCaptureServiceAdapter(
            cfg,
            on_start=lambda: called.__setitem__("start", True),
            on_stop=lambda: called.__setitem__("stop", True),
            on_mute=lambda m: called.__setitem__("mute", m),
            on_apply_config=lambda d: called.__setitem__("apply_config", d),
        )

        # Simular mensajes MQTT directamente
        adapter._handle_command({"action": "start", "msg_id": "t1", "source": "test", "params": {}})
        self.assertTrue(called.get("start"))

        adapter._handle_command({"action": "stop", "msg_id": "t2", "source": "test", "params": {}})
        self.assertTrue(called.get("stop"))

        adapter._handle_command({"action": "mute", "msg_id": "t3", "source": "test", "params": {}})
        self.assertTrue(called.get("mute"))

        adapter._handle_command({"action": "unmute", "msg_id": "t4", "source": "test", "params": {}})
        self.assertFalse(called.get("mute"))

        adapter._handle_command({"action": "apply_config", "msg_id": "t5", "source": "test",
                                  "params": {"gain_db": 3.0}})
        self.assertEqual(called.get("apply_config"), {"gain_db": 3.0})
        print(f"  ✓ Despacho de comandos MQTT funciona correctamente")

    def test_unknown_action(self):
        """Verifica que acciones desconocidas no lanzan excepción."""
        from audio_capture_service.mqtt_adapter import AudioCaptureServiceAdapter
        cfg = self._make_cfg()
        adapter = AudioCaptureServiceAdapter(cfg)
        # No debe lanzar excepción
        adapter._handle_command({"action": "hacer_cafe", "msg_id": "t99", "source": "test", "params": {}})
        print(f"  ✓ Acción desconocida manejada sin excepción")

    def test_desired_config_handler(self):
        """Verifica que config/desired dispara apply_config."""
        from audio_capture_service.mqtt_adapter import AudioCaptureServiceAdapter
        cfg = self._make_cfg()
        received = {}
        adapter = AudioCaptureServiceAdapter(
            cfg,
            on_apply_config=lambda d: received.update(d)
        )
        adapter._handle_desired_config({
            "service": "audio_in",
            "config": {"gain_db": 6.0, "muted": False},
            "ts": "2026-01-01T00:00:00Z"
        })
        self.assertEqual(received.get("gain_db"), 6.0)
        self.assertFalse(received.get("muted"))
        print(f"  ✓ Config desired handler funciona correctamente")


# ─────────────────────────────────────────────────────────────────────────────

class TestSimulationMode(unittest.TestCase):
    """Test del servicio completo en modo simulación (sin hardware ni MQTT real)."""

    def test_simulate_startup_and_shutdown(self):
        """
        Arranca el servicio en modo simulación y lo para inmediatamente.
        Verifica que el ciclo de vida completo no lanza excepciones.
        """
        import threading
        from audio_capture_service.config import AudioCaptureConfig
        from audio_capture_service.main import AudioCaptureService

        cfg = AudioCaptureConfig(
            node_id="test-sim",
            mqtt_broker="127.0.0.1",  # no conectará — OK en simulación
            dest_ip="127.0.0.1",
        )
        service = AudioCaptureService(cfg, simulate=True)

        result_holder = [None]

        def run_service():
            # Parar el servicio a los 2 segundos
            import threading
            timer = threading.Timer(2.0, service._shutdown_event.set)
            timer.start()
            result_holder[0] = service.run()

        thread = threading.Thread(target=run_service, daemon=True)
        thread.start()
        thread.join(timeout=10.0)

        self.assertFalse(thread.is_alive(), "El servicio no terminó en 10s")
        self.assertEqual(result_holder[0], 0, f"Código de salida inesperado: {result_holder[0]}")
        print(f"  ✓ Modo simulación: ciclo de vida completo OK")


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("  Audio Capture Service — Tests")
    print("=" * 60)
    print()

    # Verificar Python version
    if sys.version_info < (3, 10):
        print(f"⚠ Python {sys.version_info.major}.{sys.version_info.minor} — se recomienda 3.10+")
    else:
        print(f"✓ Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")

    # Verificar disponibilidad de dependencias opcionales
    for dep in ["paho.mqtt.client", "usb.core", "gi.repository.Gst", "sounddevice"]:
        try:
            __import__(dep.split(".")[0])
            print(f"✓ {dep} disponible")
        except ImportError:
            print(f"⚠ {dep} NO disponible (instalar según requirements.txt)")

    print()

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    test_classes = [
        TestSyntax,
        TestConfig,
        TestDeviceDiscovery,
        TestPipelineString,
        TestMqttAdapter,
        TestSimulationMode,
    ]

    for cls in test_classes:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2, stream=sys.stdout)
    result = runner.run(suite)

    print()
    if result.wasSuccessful():
        print("✅ Todos los tests pasaron")
        sys.exit(0)
    else:
        print(f"❌ {len(result.failures)} fallos, {len(result.errors)} errores")
        sys.exit(1)
