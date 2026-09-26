#!/bin/bash
# Instala/reinstala a visualizacao da camera via WebSocket.
# Uso: ./install.sh

set -e
cd "$(dirname "${BASH_SOURCE[0]}")"

sudo apt update
sudo apt install -y python3-venv python3-pip python3-picamera2

if [ ! -d venv ]; then
    python3 -m venv --system-site-packages venv
fi
./venv/bin/pip install -r requirements.txt

sudo cp camera-pi.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now camera-pi
systemctl status camera-pi --no-pager
