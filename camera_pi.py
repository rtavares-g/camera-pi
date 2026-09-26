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

from aiohttp import web, WSMsgType
from picamera2 import Picamera2  # type: ignore[import-untyped]
from picamera2.encoders import MJPEGEncoder, Quality  # type: ignore[import-untyped]
from picamera2.outputs import FileOutput  # type: ignore[import-untyped]

RAIZ = Path(__file__).resolve().parent

# So localhost: o acesso externo passa pelo Cloudflare Tunnel + Access
HOST = os.environ.get("CAMERA_HOST", "127.0.0.1")
PORTA = int(os.environ.get("CAMERA_PORTA", "8090"))
LARGURA = int(os.environ.get("CAMERA_LARGURA", "1280"))
ALTURA = int(os.environ.get("CAMERA_ALTURA", "720"))
FPS = int(os.environ.get("CAMERA_FPS", "20"))

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


def usuario(request: web.Request) -> str:
    # O login e feito pelo Cloudflare Access antes de chegar aqui; o header
    # serve so para identificar quem esta assistindo no log.
    return request.headers.get("Cf-Access-Authenticated-User-Email", "-")


async def pagina(request: web.Request):
    return web.FileResponse(RAIZ / "static" / "index.html")


async def websocket(request: web.Request):
    quem = usuario(request)

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
    app.router.add_get("/", pagina)
    app.router.add_get("/ws", websocket)
    app.on_shutdown.append(ao_desligar)
    web.run_app(app, host=HOST, port=PORTA, print=None)


if __name__ == "__main__":
    main()
