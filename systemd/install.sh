#!/usr/bin/env bash
set -euo pipefail

service_user="nexor"
install_dir="/opt/nexor/audio-binaural"
config_dir="/etc/nexor"
config_file="$config_dir/audio-binaural.json"
unit_name="itcl_audioBinaural_streaming.service"
legacy_unit="audio-capture.service"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ $EUID -ne 0 ]]; then
  echo "Ejecuta como root: sudo bash systemd/install.sh" >&2
  exit 1
fi

if ! id "$service_user" >/dev/null 2>&1; then
  useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin --groups audio "$service_user"
else
  usermod -aG audio "$service_user"
fi

install -d -o root -g root -m 0755 /opt/nexor
install -d -o "$service_user" -g audio -m 0755 "$install_dir"
rsync -a --delete \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '.git' \
  "$repo_root/audio_capture_service/" "$install_dir/audio_capture_service/"
install -m 0644 "$repo_root/README.md" "$install_dir/README.md"

if [[ ! -x "$install_dir/venv/bin/python" ]]; then
  python3 -m venv --system-site-packages "$install_dir/venv"
fi
if ! "$install_dir/venv/bin/python" -c 'import paho.mqtt.client, usb.core' 2>/dev/null; then
  "$install_dir/venv/bin/pip" install -r "$repo_root/requirements.txt"
fi

install -d -o root -g "$service_user" -m 0750 "$config_dir"
if [[ ! -f "$config_file" ]]; then
  install -o root -g "$service_user" -m 0640 \
    "$repo_root/config/audio-binaural.example.json" "$config_file"
  echo "Creado $config_file. Completa la configuración de audio antes de arrancar."
else
  chown root:"$service_user" "$config_file"
  chmod 0640 "$config_file"
fi

install -o root -g root -m 0644 "$repo_root/systemd/itcl_audioBinaural_streaming.service" \
  "/etc/systemd/system/$unit_name"
if systemctl cat "$legacy_unit" >/dev/null 2>&1; then
  systemctl disable "$legacy_unit" || true
  rm -f "/etc/systemd/system/$legacy_unit"
fi
systemctl daemon-reload
systemctl enable "$unit_name"
systemd-analyze verify "/etc/systemd/system/$unit_name"

echo "Instalación terminada. Revisa $config_file y arranca con:"
echo "  sudo systemctl start $unit_name"
