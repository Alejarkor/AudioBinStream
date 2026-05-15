#!/bin/bash

# Dirección IP y puerto del receptor en Unity
IP_DESTINO="192.168.0.20"
PUERTO_DESTINO=1234

# Tamaño del buffer (en bytes) y latencia (en microsegundos)
BUFFER_SIZE=2048
LATENCIA=10000  # 10 milisegundos

# Frecuencia de muestreo (sample rate) en Hz
SAMPLE_RATE=48000  # Puedes cambiar esto según tus necesidades

# Configuración de GStreamer para capturar y enviar audio estéreo del micrófono USB
gst-launch-1.0 -v alsasrc device=hw:2,0 buffer-time=$LATENCIA ! \
audioconvert ! \
"audio/x-raw, format=S24LE, rate=$SAMPLE_RATE, channels=2" ! \
audioresample ! \
queue max-size-buffers=0 max-size-time=0 max-size-bytes=$BUFFER_SIZE ! \
wavenc ! \
udpsink host=$IP_DESTINO port=$PUERTO_DESTINO

