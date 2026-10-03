#!/bin/bash
# Instala/reinstala o MediaMTX (dono da camera) e o painel + Sinric Pro +
# gravacao por movimento (ffmpeg).
# Uso: ./install.sh   (credenciais da Sinric em .env)

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
sudo mkdir -p /etc/mediamtx
sudo cp mediamtx.yml /etc/mediamtx/mediamtx.yml

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
