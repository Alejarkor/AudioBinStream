#!/bin/bash

# IP y puerto donde se recibe el audio
IP="0.0.0.0"
PUERTO=1235

# Frecuencia de muestreo y formato de audio
SAMPLE_RATE=44100  # Tasa de muestreo

# Reproducir el audio recibido
gst-launch-1.0 -v udpsrc port=$PUERTO caps="audio/x-raw,format=S16LE,rate=$SAMPLE_RATE,channels=1" ! \
queue ! \
audioconvert ! \
alsasink device=plughw:2,0