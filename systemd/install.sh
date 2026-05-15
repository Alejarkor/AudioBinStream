#!/bin/bash
# install.sh — Instala el servicio de captura de audio en la Jetson Orin
# Ejecutar como root: sudo bash install.sh

set -e

INSTALL_DIR="/opt/nexor/audio_capture_service"
CONFIG_DIR="/etc/nexor"
SERVICE_NAME="audio-capture"
USER="nexor"

echo "=== Instalando Audio Capture Service ==="

# ── 1. Crear usuario del servicio (si no existe) ──────────────────────────
if ! id "$USER" &>/dev/null; then
    useradd -r -s /usr/sbin/nologin -G audio "$USER"
    echo "Usuario '$USER' creado"
else
    # Asegurar que está en el grupo audio
    usermod -aG audio "$USER"
    echo "Usuario '$USER' ya existe — añadido a grupo audio"
fi

# ── 2. Dependencias del sistema ───────────────────────────────────────────
echo "Instalando dependencias del sistema..."
apt-get update -qq
apt-get install -y \
    python3-gi \
    python3-gst-1.0 \
    gstreamer1.0-alsa \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-base \
    alsa-utils \
    python3-usb \
    python3-pip

# ── 3. Dependencias Python ────────────────────────────────────────────────
echo "Instalando dependencias Python..."
pip3 install --break-system-packages paho-mqtt

# ── 4. Instalar el servicio ───────────────────────────────────────────────
echo "Copiando ficheros del servicio..."
mkdir -p "$INSTALL_DIR"
cp -r "$(dirname "$0")/../audio_capture_service/"* "$INSTALL_DIR/"
chown -R "$USER:$USER" "$INSTALL_DIR"

# ── 5. Crear directorio de configuración ──────────────────────────────────
mkdir -p "$CONFIG_DIR"
chown root:nexor "$CONFIG_DIR"
chmod 750 "$CONFIG_DIR"

# ── 6. Crear fichero de env si no existe ──────────────────────────────────
ENV_FILE="$CONFIG_DIR/audio_capture.env"
if [ ! -f "$ENV_FILE" ]; then
    cat > "$ENV_FILE" << 'EOF'
# Variables de entorno para el servicio de captura de audio
# EDITAR antes de arrancar el servicio

MQTT_BROKER=192.168.1.100
MQTT_PORT=1883
MQTT_USER=nexor_audio_in
MQTT_PASSWORD=CAMBIAR_ESTO
NODE_ID=nexor-01
AUDIO_DEST_IP=192.168.0.20
AUDIO_DEST_PORT=5004
EOF
    chmod 640 "$ENV_FILE"
    chown root:nexor "$ENV_FILE"
    echo "Fichero de env creado en $ENV_FILE — EDÍTALO antes de arrancar"
else
    echo "Fichero de env ya existe: $ENV_FILE"
fi

# ── 7. Instalar unidad systemd ────────────────────────────────────────────
echo "Instalando unidad systemd..."
cp "$(dirname "$0")/audio-capture.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

echo ""
echo "=== Instalación completada ==="
echo ""
echo "Pasos siguientes:"
echo "  1. Edita $ENV_FILE con las credenciales MQTT correctas"
echo "  2. sudo systemctl start $SERVICE_NAME"
echo "  3. sudo journalctl -u $SERVICE_NAME -f"
echo ""
