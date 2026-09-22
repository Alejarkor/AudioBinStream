# Migración desde la instalación anterior

La versión anterior separaba `audio_capture.json`, `node_runtime.json` y un
fichero `.env`. La versión actual usa solo `/etc/nexor/audio-binaural.json`.

1. Ejecuta `sudo python3 scripts/migrate_legacy_config.py`, o copia
   `config/audio-binaural.example.json` si partes de una instalación nueva.
2. Revisa `node_id`, broker, usuario, contraseña y opciones TLS.
3. Revisa los parámetros de audio que quieras conservar.
4. No copies `alsa_device_override`; el RØDE se descubre dinámicamente.
5. Instala con `sudo bash systemd/install.sh`.
6. Configura en `serviceController` el alias lógico `audio_binaural` para la
   unidad `audio-capture.service`.

Los antiguos ficheros se pueden conservar como referencia hasta validar la
primera sesión MQTT y la recepción de audio. No deben permanecer con secretos
en el repositorio ni con permisos legibles globalmente.
