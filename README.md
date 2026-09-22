# Audio Binaural

Servicio residente para Jetson que captura el RØDE AI-Micro y transmite audio a
un destino UDP configurado mediante MQTT. `systemd` posee el proceso y el
`serviceController` posee su ciclo de vida.

## Responsabilidades

```text
serviceController ── start / stop / restart ──> itcl_audioBinaural_streaming.service
MQTT audio_binaural_streaming ── standby, resume, mute, config, target ──> servicio activo
Audio Binaural ── raw UDP o RTP ──> receptor configurado
```

El servicio no acepta `start`, `stop` ni `restart` por su topic propio: un
proceso detenido no puede recibirlos. El controlador genérico debe mapear el
nombre lógico `audio_binaural_streaming` a
`itcl_audioBinaural_streaming.service` en su lista cerrada de servicios
autorizados. La unidad conserva aliases de los nombres anteriores durante la
migración.

## Configuración

La configuración propia de audio es `/etc/nexor/audio-binaural.json`. El nodo,
broker, credenciales y TLS se leen exclusivamente de `/etc/nexor/mqtt.env`, que
comparten vídeo, captura, reproducción y `serviceController`. Debe ser
`root:nexor`, modo `0640`, y no debe versionarse.

Los cambios MQTT nunca reescriben ese archivo. El broker debe retener los topics
`config/desired` y `stream_target/desired`; así el estado deseado se reaplica
después de reiniciar la Jetson.

Campos importantes:

- `device_name`: identifica el RØDE por nombre, no por índice ALSA ni USB.
- `protocol`: `raw_udp` o `rtp`.
- `wait_for_mqtt_target`: impide capturar/empezar a emitir hasta recibir un
  destino válido por MQTT.

## MQTT

Base: `nexor/v1/nodes/{node_id}/services/audio_binaural_streaming`.

| Topic | Uso |
| --- | --- |
| `state` | Estado retenido: `WAITING_TARGET`, `RUNNING`, `STANDBY`, `ERROR`… |
| `capabilities` | Acciones y esquema soportados. |
| `config/desired` | Solo `gain_db`, `muted`, `rode_mode`, formato y transporte. |
| `config/reported` | Configuración efectiva sin credenciales. |
| `stream_target/desired` | `{ "sink": { "ip": "x.x.x.x", "port": 1234, "transport": "raw_udp" } }` |
| `stream_target/reported` | Destino usado por el pipeline. |
| `events` | Errores y confirmaciones. |

Comandos válidos en `cmd`: `resume`, `standby`, `mute`, `unmute`, `get_state` y
`apply_config`. El procesamiento se serializa en el hilo principal; el callback
MQTT no toca directamente el pipeline GStreamer.

## Instalación

```bash
sudo bash systemd/install.sh
sudoedit /etc/nexor/audio-binaural.json
sudo systemctl start itcl_audioBinaural_streaming.service
sudo journalctl -u itcl_audioBinaural_streaming.service -f
```

El código se instala en `/opt/nexor/audio-binaural`; no se ejecuta desde el
checkout. El instalador crea un entorno virtual que puede usar las bindings
GStreamer del sistema.

## Operación

El RØDE se descubre con su nombre ALSA. Si cambia de puerto USB, también puede
cambiar su índice de tarjeta, pero el servicio lo vuelve a resolver antes de
crear el pipeline. No fijar `hw:2,0` en configuración.

Para una red controlada puede usarse `raw_udp`. `rtp` es preferible cuando el
receptor necesita timestamps, secuencia de paquetes o buffer de jitter.

## Verificación

```bash
python3 test_service.py
systemd-analyze verify systemd/itcl_audioBinaural_streaming.service
```

La prueba de integración final debe comprobar: servicio activo, conexión MQTT,
estado `WAITING_TARGET`, envío de un destino retenido y recepción del audio.
