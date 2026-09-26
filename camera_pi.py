#!/usr/bin/env python3
"""
Visualizacao da camera do Raspberry Pi via WebSocket.

A camera (Picamera2 + encoder MJPEG por hardware) so fica ligada enquanto
houver alguem conectado. Cada frame JPEG e enviado como mensagem binaria
para todos os clientes em /ws; a pagina em / desenha os frames num <img>.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import threading
from pathlib import Path

import jwt
from aiohttp import web, WSMsgType
from picamera2 import Picamera2
from picamera2.encoders import MJPEGEncoder, Quality
from picamera2.outputs import FileOutput

RAIZ = Path(__file__).resolve().parent

# So localhost: o acesso externo passa pelo Cloudflare Tunnel + Access
HOST = os.environ.get("CAMERA_HOST", "127.0.0.1")
PORTA = int(os.environ.get("CAMERA_PORTA", "8090"))
LARGURA = int(os.environ.get("CAMERA_LARGURA", "1280"))
ALTURA = int(os.environ.get("CAMERA_ALTURA", "720"))
FPS = int(os.environ.get("CAMERA_FPS", "20"))
# Cloudflare Access: time (ex: "meutime" de meutime.cloudflareaccess.com)
# e o Application Audience (AUD) Tag da aplicacao. Com os dois definidos,
# toda requisicao precisa do JWT valido que o Access injeta.
CF_TEAM = os.environ.get("CF_ACCESS_TEAM", "")
CF_AUD = os.environ.get("CF_ACCESS_AUD", "")

log = logging.getLogger("camera-pi")


class SaidaFrames(io.BufferedIOBase):
    """Recebe frames do encoder (thread da camera) e entrega ao asyncio."""

    def __init__(self, loop: asyncio.AbstractEventLoop, ao_receber):
        self.loop = loop
        self.ao_receber = ao_receber

    def write(self, buf):
        self.loop.call_soon_threadsafe(self.ao_receber, bytes(buf))
        return len(buf)


class Camera:
    def __init__(self):
        self.clientes: set[web.WebSocketResponse] = set()
        self.picam: Picamera2 | None = None
        self.lock = threading.Lock()
        self.ultimo_frame: bytes | None = None
        self.novo_frame = asyncio.Event()

    def _ligar(self, loop):
        with self.lock:
            if self.picam is not None:
                return
            picam = Picamera2()
            picam.configure(picam.create_video_configuration(
                main={"size": (LARGURA, ALTURA)},
                controls={"FrameRate": FPS},
            ))
            picam.start_recording(
                MJPEGEncoder(), FileOutput(SaidaFrames(loop, self._frame)),
                quality=Quality.MEDIUM,
            )
            self.picam = picam
            log.info("Camera ligada (%dx%d @ %d fps)", LARGURA, ALTURA, FPS)

    def _desligar(self):
        with self.lock:
            if self.picam is None:
                return
            try:
                self.picam.stop_recording()
            finally:
                self.picam.close()
                self.picam = None
                self.ultimo_frame = None
            log.info("Camera desligada (sem clientes)")

    def _frame(self, frame: bytes):
        self.ultimo_frame = frame
        self.novo_frame.set()
        self.novo_frame = asyncio.Event()

    async def entrar(self, ws):
        self.clientes.add(ws)
        if len(self.clientes) == 1:
            await asyncio.to_thread(self._ligar, asyncio.get_running_loop())

    async def sair(self, ws):
        self.clientes.discard(ws)
        if not self.clientes:
            await asyncio.to_thread(self._desligar)


class AccessCloudflare:
    """Valida o JWT (Cf-Access-Jwt-Assertion) emitido pelo Cloudflare Access."""

    def __init__(self, time: str, aud: str):
        self.emissor = f"https://{time}.cloudflareaccess.com"
        self.aud = aud
        self.chaves = jwt.PyJWKClient(f"{self.emissor}/cdn-cgi/access/certs",
                                      cache_keys=True, lifespan=3600)

    def _validar(self, token: str) -> dict:
        chave = self.chaves.get_signing_key_from_jwt(token)
        return jwt.decode(token, chave.key, algorithms=["RS256"],
                          audience=self.aud, issuer=self.emissor)

    async def usuario(self, request: web.Request) -> str:
        token = (request.headers.get("Cf-Access-Jwt-Assertion")
                 or request.cookies.get("CF_Authorization"))
        if not token:
            raise web.HTTPForbidden(text="acesso somente via Cloudflare Access")
        try:
            dados = await asyncio.to_thread(self._validar, token)
        except jwt.PyJWTError as e:
            log.warning("JWT do Access recusado (%s): %s", request.remote, e)
            raise web.HTTPForbidden(text="token do Cloudflare Access invalido")
        return dados.get("email") or dados.get("sub", "?")


async def usuario(request: web.Request) -> str:
    access: AccessCloudflare | None = request.app["access"]
    return await access.usuario(request) if access else "-"


async def pagina(request: web.Request):
    await usuario(request)
    return web.FileResponse(RAIZ / "static" / "index.html")


async def websocket(request: web.Request):
    quem = await usuario(request)

    camera: Camera = request.app["camera"]
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    log.info("Cliente conectado: %s", quem)

    try:
        await camera.entrar(ws)
    except Exception as e:
        log.exception("Falha ao ligar a camera")
        await ws.close(message=f"erro na camera: {e}".encode()[:120])
        await camera.sair(ws)
        return ws

    async def enviar():
        # Sempre manda o frame mais recente; cliente lento pula frames
        # em vez de acumular fila.
        while not ws.closed:
            await camera.novo_frame.wait()
            frame = camera.ultimo_frame
            if frame:
                await ws.send_bytes(frame)

    tarefa = asyncio.create_task(enviar())
    try:
        async for msg in ws:
            if msg.type == WSMsgType.ERROR:
                break
    finally:
        tarefa.cancel()
        await camera.sair(ws)
        log.info("Cliente desconectado: %s", quem)
    return ws


async def ao_desligar(app: web.Application):
    for ws in list(app["camera"].clientes):
        await ws.close()
    await asyncio.to_thread(app["camera"]._desligar)


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    app = web.Application()
    app["camera"] = Camera()
    if CF_TEAM and CF_AUD:
        app["access"] = AccessCloudflare(CF_TEAM, CF_AUD)
        log.info("Cloudflare Access ativo (time %s)", CF_TEAM)
    else:
        app["access"] = None
        log.warning("CF_ACCESS_TEAM/CF_ACCESS_AUD nao definidos - "
                    "sem validacao do Cloudflare Access")
    app.router.add_get("/", pagina)
    app.router.add_get("/ws", websocket)
    app.on_shutdown.append(ao_desligar)
    web.run_app(app, host=HOST, port=PORTA, print=None)


if __name__ == "__main__":
    main()
