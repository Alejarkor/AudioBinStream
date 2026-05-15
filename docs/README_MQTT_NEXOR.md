# Nexor MQTT + Audio Binaural (UDP)

## Modelo correcto
- La **Jetson** actuaba como **origen del stream**.
- **Unity** actuaba como **receptor UDP**.
- En `raw_udp` y `rtp` no existía un cliente que se conectara al servidor; lo que existía era un **destino IP:puerto** al que la Jetson emitía.

## Configuración base
- Namespace MQTT: `nexor/v1/nodes/{nodeId}/services/audio_binaural/...`
- Configuración común del nodo: `config/node_runtime.json`
- Configuración propia del servicio: `config/audio_capture.json`
- Transporte recomendado para pruebas: `raw_udp`

## Topics principales
```text
nexor/v1/nodes/nexor-01/services/audio_binaural/state
nexor/v1/nodes/nexor-01/services/audio_binaural/config/reported
nexor/v1/nodes/nexor-01/services/audio_binaural/config/desired
nexor/v1/nodes/nexor-01/services/audio_binaural/endpoint
nexor/v1/nodes/nexor-01/services/audio_binaural/stream_target/desired
nexor/v1/nodes/nexor-01/services/audio_binaural/stream_target/reported
nexor/v1/nodes/nexor-01/services/audio_binaural/events
```

## Qué significaba cada topic
- `state`: estado operativo del servicio.
- `config/reported`: configuración realmente activa.
- `config/desired`: cambios generales de configuración.
- `endpoint`: descripción del stream actual. En UDP se anunciaba como `outbound_push`.
- `stream_target/desired`: topic específico para notificar **a qué IP y puerto** debía emitir la Jetson.
- `stream_target/reported`: confirmación del destino actualmente configurado.

## Arranque manual de prueba
```bash
export NEXOR_NODE_CONFIG=./config/node_runtime.json
python3 -m audio_capture_service.main --config ./config/audio_capture.json --log-level DEBUG
```

## Probar que publica estado
```bash
mosquitto_sub -h 127.0.0.1 -t 'nexor/v1/nodes/nexor-01/services/audio_binaural/#' -v
```

## Cambiar el destino UDP por MQTT
```bash
mosquitto_pub -h 127.0.0.1   -t 'nexor/v1/nodes/nexor-01/services/audio_binaural/stream_target/desired'   -m '{"sink":{"ip":"192.168.1.120","port":1234,"transport":"raw_udp"}}'
```

## Cambiar parámetros generales por MQTT
```bash
mosquitto_pub -h 127.0.0.1   -t 'nexor/v1/nodes/nexor-01/services/audio_binaural/config/desired'   -m '{"config":{"rode_mode":"split","gain_db":3.0}}'
```

## Configuración de arranque para pruebas
También podía arrancarse ya con el destino seteado en `config/audio_capture.json`:
```json
{
  "protocol": "raw_udp",
  "dest_ip": "192.168.1.120",
  "dest_port": 1234
}
```

## Nota sobre el cliente Unity
Unity no debía conectarse a un servidor TCP si el transporte elegido era UDP. Debía abrir un puerto local y escuchar los paquetes enviados por la Jetson.
