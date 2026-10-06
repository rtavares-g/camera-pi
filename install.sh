#!/bin/bash
# Instala/reinstala o MediaMTX (dono da camera) e o painel + gravacao por
# movimento (ffmpeg). O Home Assistant le a camera por RTSP com usuario e
# senha proprios, guardados em ~/.config/camera-pi/rtsp-ha.json (fora do git).
# Uso: ./install.sh

set -e
cd "$(dirname "${BASH_SOURCE[0]}")"

MEDIAMTX_VERSAO=v1.21.1

sudo apt update
sudo apt install -y python3-venv python3-pip ffmpeg

if [ ! -x /usr/local/bin/mediamtx ]; then
    tmp=$(mktemp -d)
    curl -sL "https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VERSAO}/mediamtx_${MEDIAMTX_VERSAO}_linux_arm64.tar.gz" | tar xz -C "$tmp"
    sudo install -m 755 "$tmp/mediamtx" /usr/local/bin/mediamtx
    rm -rf "$tmp"
fi
RTSP_HA="$HOME/.config/camera-pi/rtsp-ha.json"
if [ ! -f "$RTSP_HA" ]; then
    read -rp "IP do Home Assistant [192.168.1.211]: " HA_IP
    mkdir -p "$(dirname "$RTSP_HA")"
    ( umask 077; printf '{"usuario": "homeassistant", "senha": "%s", "ip": "%s"}\n' \
        "$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')" "${HA_IP:-192.168.1.211}" > "$RTSP_HA" )
    echo "==> senha RTSP do HA gerada em $RTSP_HA (use em rtsp://usuario:senha@<ip do Pi>:8554/cam)"
fi
HA_RTSP_USUARIO=$(python3 -c "import json; print(json.load(open('$RTSP_HA'))['usuario'])")
HA_RTSP_SENHA=$(python3 -c "import json; print(json.load(open('$RTSP_HA'))['senha'])")
HA_IP=$(python3 -c "import json; print(json.load(open('$RTSP_HA'))['ip'])")
sudo mkdir -p /etc/mediamtx
sed -e "s|__HA_RTSP_USUARIO__|$HA_RTSP_USUARIO|" -e "s|__HA_RTSP_SENHA__|$HA_RTSP_SENHA|" -e "s|__HA_IP__|$HA_IP|" \
    mediamtx.yml | sudo tee /etc/mediamtx/mediamtx.yml > /dev/null
sudo chown "root:$USER" /etc/mediamtx/mediamtx.yml && sudo chmod 640 /etc/mediamtx/mediamtx.yml

if [ ! -d venv ]; then
    python3 -m venv venv
fi
./venv/bin/pip install -r requirements.txt

for servico in mediamtx.service camera-pi.service; do
    sed -e "s|__USER__|$USER|g" -e "s|__HOME__|$HOME|g" "$servico" | sudo tee "/etc/systemd/system/$servico" > /dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable mediamtx camera-pi
sudo systemctl restart mediamtx camera-pi
systemctl status mediamtx camera-pi --no-pager
