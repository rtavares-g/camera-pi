#!/usr/bin/env python3
"""
Visualizacao da camera do Raspberry Pi + camera na Sinric Pro.

Quem controla a camera e o MediaMTX (/etc/mediamtx/mediamtx.yml, path
"cam"), que so a liga enquanto houver alguem assistindo. Este servico:

- serve o painel em / e repassa o HLS do MediaMTX em /cam/ (o painel chega
  aqui pelo Cloudflare Tunnel + Access);
- conecta na Sinric Pro como dispositivo Camera e responde os pedidos de
  WebRTC da Alexa/Google Home repassando a oferta SDP para o WHEP do
  MediaMTX. O video WebRTC vai direto do MediaMTX para o aparelho.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from pathlib import Path

import aiohttp
from aiohttp import web
from multidict import CIMultiDict
from sinricpro import SinricPro, SinricProConfig  # type: ignore[import-untyped]
from sinricpro.devices import SinricProCamera  # type: ignore[import-untyped]

RAIZ = Path(__file__).resolve().parent


def carregar_env(caminho: Path) -> None:
    """Le o .env sem depender de biblioteca externa. Variaveis ja presentes
    no ambiente vencem o arquivo."""
    if not caminho.is_file():
        return
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, _, valor = linha.partition("=")
        chave = chave.strip()
        valor = valor.strip().strip('"').strip("'")
        if chave and chave not in os.environ:
            os.environ[chave] = valor


carregar_env(RAIZ / ".env")

# So localhost: o acesso externo passa pelo Cloudflare Tunnel + Access
HOST = os.environ.get("CAMERA_HOST", "127.0.0.1")
PORTA = int(os.environ.get("CAMERA_PORTA", "8090"))
MEDIAMTX_HLS = os.environ.get("MEDIAMTX_HLS", "http://127.0.0.1:8888")
MEDIAMTX_WHEP = os.environ.get("MEDIAMTX_WHEP", "http://127.0.0.1:8889/cam/whep")

SINRIC_DEVICE_ID = os.environ.get("SINRIC_DEVICE_ID", "")
SINRIC_APP_KEY = os.environ.get("SINRIC_APP_KEY", "")
SINRIC_APP_SECRET = os.environ.get("SINRIC_APP_SECRET", "")
SINRIC_DEBUG = os.environ.get("SINRIC_DEBUG", "0") == "1"

log = logging.getLogger("camera-pi")

# Headers que nao devem ser copiados de uma resposta para outra num proxy
HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "content-length",
              "content-encoding", "upgrade", "proxy-authenticate",
              "proxy-authorization", "te", "trailer"}


def usuario(request: web.Request) -> str:
    # O login e feito pelo Cloudflare Access antes de chegar aqui; o header
    # serve so para identificar quem esta assistindo no log.
    return request.headers.get("Cf-Access-Authenticated-User-Email", "-")


async def pagina(request: web.Request):
    log.info("Painel aberto: %s", usuario(request))
    return web.FileResponse(RAIZ / "static" / "index.html")


async def hls(request: web.Request):
    """Repassa /cam/* para o HLS do MediaMTX (inclui a query string, que o
    Low-Latency HLS usa para segurar a playlist ate a proxima parte)."""
    sessao: aiohttp.ClientSession = request.app["http"]
    url = f"{MEDIAMTX_HLS}{request.rel_url}"
    try:
        async with sessao.get(url, allow_redirects=False,
                              headers={"Cookie": request.headers.get("Cookie", "")}) as resp:
            saida = web.StreamResponse(status=resp.status, headers=CIMultiDict(
                (k, v) for k, v in resp.headers.items() if k.lower() not in HOP_BY_HOP
            ))
            await saida.prepare(request)
            async for bloco in resp.content.iter_chunked(64 * 1024):
                await saida.write(bloco)
            await saida.write_eof()
            return saida
    except aiohttp.ClientError as e:
        log.warning("MediaMTX indisponivel (%s)", e)
        raise web.HTTPBadGateway(text="MediaMTX indisponivel")


class Sinric:
    """Camera na Sinric Pro (SDK oficial sinricpro)."""

    def __init__(self, http: aiohttp.ClientSession):
        self.http = http
        self.conectado = False

    @property
    def configurado(self) -> bool:
        return bool(SINRIC_DEVICE_ID and SINRIC_APP_KEY and SINRIC_APP_SECRET)

    async def iniciar(self) -> None:
        if not self.configurado:
            log.warning("SINRIC: credenciais nao configuradas")
            return

        camera = SinricProCamera(SINRIC_DEVICE_ID)
        camera.on_get_webrtc_answer(self._webrtc_answer)
        camera.on_power_state(self._power_state)

        sinric_pro = SinricPro.get_instance()
        sinric_pro.on_connected(self._ao_conectar)
        sinric_pro.on_disconnected(self._ao_desconectar)
        sinric_pro.add(camera)

        log.info("SINRIC: conectando...")
        await sinric_pro.begin(SinricProConfig(
            app_key=SINRIC_APP_KEY,
            app_secret=SINRIC_APP_SECRET,
            debug=SINRIC_DEBUG,
            # O controle local (UDP + mDNS) ja e feito pelos outros servicos
            # da Sinric neste Pi; a camera so precisa da nuvem.
            local_control=False,
            mdns=False,
        ))

    async def _webrtc_answer(self, device_id: str, oferta: str) -> tuple[bool, str]:
        log.info("SINRIC: pedido de video WebRTC")
        sdp = base64.b64decode(oferta)
        self._log_sdp("oferta", sdp)
        try:
            async with self.http.post(
                MEDIAMTX_WHEP, data=sdp, headers={"Content-Type": "application/sdp"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                corpo = await resp.read()
                if resp.status != 201:
                    log.warning("SINRIC: WHEP recusou (%d): %s", resp.status,
                                corpo[:200].decode(errors="replace"))
                    return False, ""
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning("SINRIC: MediaMTX indisponivel (%s)", e)
            return False, ""
        self._log_sdp("resposta", corpo)
        return True, base64.b64encode(corpo).decode()

    @staticmethod
    def _log_sdp(nome: str, sdp: bytes) -> None:
        # So as linhas que importam para diagnosticar conexao/codec.
        linhas = [l for l in sdp.decode(errors="replace").splitlines()
                  if l.startswith(("a=candidate", "m=", "a=rtpmap", "a=fmtp", "a=ice-options",
                                   "a=setup", "a=sendonly", "a=recvonly", "a=sendrecv", "c="))]
        log.debug("SINRIC: SDP %s:\n  %s", nome, "\n  ".join(linhas))

    async def _power_state(self, ligado: bool) -> bool:
        # A camera liga sozinha sob demanda; so confirmamos o comando.
        log.info("SINRIC: power %s", "on" if ligado else "off")
        return True

    def _ao_conectar(self) -> None:
        self.conectado = True
        log.info("SINRIC: conectado")

    def _ao_desconectar(self) -> None:
        self.conectado = False
        log.warning("SINRIC: desconectado")

    async def vigiar_reconexao(self) -> None:
        """Contorna um bug do SDK: se a 1a tentativa de reconexao falhar,
        ele desiste para sempre. Aqui forcamos nova tentativa enquanto a
        conexao estiver caida."""
        while True:
            await asyncio.sleep(30)
            if not self.configurado or self.conectado:
                continue
            sp = SinricPro.get_instance()
            if sp.websocket and not sp.websocket.is_connected():
                log.info("SINRIC: sem conexao ha um tempo, forcando nova tentativa")
                sp.websocket.schedule_reconnect()


async def ao_iniciar(app: web.Application):
    app["http"] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=5))
    sinric = Sinric(app["http"])
    await sinric.iniciar()
    app["vigia"] = asyncio.create_task(sinric.vigiar_reconexao())


async def ao_desligar(app: web.Application):
    app["vigia"].cancel()
    await app["http"].close()


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    app = web.Application()
    app.router.add_get("/", pagina)
    app.router.add_get("/cam/{resto:.*}", hls)
    app.on_startup.append(ao_iniciar)
    app.on_cleanup.append(ao_desligar)
    web.run_app(app, host=HOST, port=PORTA, print=None)


if __name__ == "__main__":
    main()
