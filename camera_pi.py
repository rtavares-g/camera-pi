#!/usr/bin/env python3
"""
Visualizacao da camera do Raspberry Pi (painel, gravacoes, LED).

Quem controla a camera e o MediaMTX (/etc/mediamtx/mediamtx.yml, path
"cam"), que so a liga enquanto houver alguem assistindo. Este servico:

- serve o painel em / e repassa o HLS do MediaMTX em /cam/ (o painel chega
  aqui pelo Cloudflare Tunnel + Access);
- o Home Assistant le a camera direto do MediaMTX por RTSP (usuario e
  senha proprios, ver mediamtx.yml); Alexa/Google chegam pelo HA;
- grava os eventos de movimento (gravador.py) e serve o painel de
  gravacoes em /gravacoes, com a API em /api/;
- acende o LED vermelho da camera enquanto alguem assiste (painel ou HA).
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from pathlib import Path

import aiohttp
from aiohttp import web
from aiohttp.abc import AbstractAccessLogger
from multidict import CIMultiDict

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

from gravador import Gravador  # noqa: E402  (le o .env no import)

# So localhost: o acesso externo passa pelo Cloudflare Tunnel + Access
HOST = os.environ.get("CAMERA_HOST", "127.0.0.1")
PORTA = int(os.environ.get("CAMERA_PORTA", "8090"))
MEDIAMTX_HLS = os.environ.get("MEDIAMTX_HLS", "http://127.0.0.1:8888")
MEDIAMTX_API = os.environ.get("MEDIAMTX_API", "http://127.0.0.1:9997")
# LED da camera (ov5647): linha CAM_GPIO1 do expansor de GPIO do Pi 3B+
LED_CHIP = os.environ.get("CAMERA_LED_CHIP", "gpiochip1")
LED_LINHA = os.environ.get("CAMERA_LED_LINHA", "6")

log = logging.getLogger("camera-pi")

# Headers que nao devem ser copiados de uma resposta para outra num proxy
HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "content-length",
              "content-encoding", "upgrade", "proxy-authenticate",
              "proxy-authorization", "te", "trailer"}


class AccessLog(AbstractAccessLogger):
    """So loga o que muda algo ou deu erro: o HLS e o painel de gravacoes
    fazem varias requisicoes por segundo."""

    def log(self, request, response, time):  # noqa: A002
        if request.method != "GET" or response.status >= 400:
            self.logger.info("%s %s %s -> %d (%s)", usuario(request), request.method,
                             request.path, response.status, f"{time:.2f}s")


def usuario(request: web.Request) -> str:
    # O login e feito pelo Cloudflare Access antes de chegar aqui; o header
    # serve so para identificar quem esta assistindo no log.
    return request.headers.get("Cf-Access-Authenticated-User-Email", "-")


async def pagina(request: web.Request):
    log.info("Painel aberto: %s", usuario(request))
    return web.FileResponse(RAIZ / "static" / "index.html")


async def pagina_gravacoes(request: web.Request):
    return web.FileResponse(RAIZ / "static" / "gravacoes.html")


async def api_gravacoes(request: web.Request):
    g: Gravador = request.app["gravador"]
    return web.json_response({"dias": g.eventos(), "estado": g.estado()})


async def api_estado(request: web.Request):
    return web.json_response(request.app["gravador"].estado())


async def api_config(request: web.Request):
    g: Gravador = request.app["gravador"]
    config = g.salvar_config(await request.json())
    log.info("Config de gravacao alterada por %s", usuario(request))
    return web.json_response(config)


async def api_privacidade(request: web.Request):
    """Alterna modo privacidade: desliga câmera e gravação."""
    g: Gravador = request.app["gravador"]
    corpo = await request.json()
    ativar = corpo.get("ativo", True)
    estado = g.salvar_config({"privacidade": ativar})
    privado = estado.get("privacidade", False)
    # Desliga/liga o MediaMTX (que gerencia a câmera e WebRTC)
    cmd = "stop" if privado else "restart"
    try:
        subprocess.run(["sudo", "systemctl", cmd, "mediamtx"], check=True,
                       capture_output=True, timeout=10)
        log.info("Privacidade %s por %s (mediamtx %s)", 
                 "ativada" if privado else "desativada", usuario(request), cmd)
    except Exception as e:
        log.warning("Falha ao %s o mediamtx: %s", cmd, e)
    return web.json_response({"privacidade": privado})


async def api_apagar(request: web.Request):
    """Apaga eventos: {"ids": ["AAAA-MM-DD/HH-MM-SS", ...]} ou {"dia": "AAAA-MM-DD"}."""
    g: Gravador = request.app["gravador"]
    corpo = await request.json()
    ids = list(corpo.get("ids", []))
    if corpo.get("dia"):
        ids += [e["id"] for d in g.eventos() if d["dia"] == corpo["dia"] for e in d["eventos"]]
    apagados = sum(1 for i in ids if isinstance(i, str) and g.apagar(i))
    log.info("%s apagou %d gravacao(oes)", usuario(request), apagados)
    return web.json_response({"apagados": apagados})


async def arquivo_gravacao(request: web.Request):
    caminho = Gravador.caminho(f"{request.match_info['dia']}/{request.match_info['hora']}",
                               request.match_info["ext"])
    if caminho is None:
        raise web.HTTPNotFound()
    headers = {"Cache-Control": "private, max-age=86400"}
    if "baixar" in request.query:
        headers["Content-Disposition"] = (
            f'attachment; filename="camera_{request.match_info["dia"]}_{caminho.name}"')
    return web.FileResponse(caminho, headers=headers)


async def hls(request: web.Request):
    """Repassa /cam/* para o HLS do MediaMTX (inclui a query string, que o
    Low-Latency HLS usa para segurar a playlist ate a proxima parte)."""
    sessao: aiohttp.ClientSession = request.app["http"]
    led: Led = request.app["led"]
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
            if resp.status < 400:
                led.hls_visto()
            return saida
    except aiohttp.ClientError as e:
        log.warning("MediaMTX indisponivel (%s)", e)
        raise web.HTTPBadGateway(text="MediaMTX indisponivel")


class Led:
    """Acende o LED da camera enquanto alguem assiste:
    - painel (HLS): o player pede partes a cada fracao de segundo por este
      proxy, entao alguns segundos sem pedido = aba fechada. A sessao HLS do
      MediaMTX so fecha por inatividade ~40 s depois, tarde demais;
    - Home Assistant (e Alexa/Google pelo HA): sessoes RTSP de leitura
      vindas da rede, na API do MediaMTX. O detector de movimento tambem
      le por RTSP, mas do localhost, e nao conta."""

    HLS_OCIOSO = 4  # segundos sem pedido HLS para considerar a aba fechada

    def __init__(self, http: aiohttp.ClientSession):
        self.http = http
        self.aceso: bool | None = None
        self.ultimo_hls = float("-inf")

    def hls_visto(self) -> None:
        self.ultimo_hls = time.monotonic()
        self.acender(True)

    def acender(self, aceso: bool) -> None:
        if aceso == self.aceso:
            return
        # -t 0: aplica o valor e sai; o expansor mantem o estado
        r = subprocess.run(["gpioset", "-t", "0", "-c", LED_CHIP, f"{LED_LINHA}={int(aceso)}"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            log.warning("LED: gpioset falhou: %s", r.stderr.strip())
            return
        self.aceso = aceso
        log.info("LED da camera %s", "aceso" if aceso else "apagado")

    async def espectadores(self) -> int:
        async with self.http.get(f"{MEDIAMTX_API}/v3/rtspsessions/list",
                                 timeout=aiohttp.ClientTimeout(total=3)) as r:
            r.raise_for_status()
            sessoes = (await r.json()).get("items") or []
        return sum(1 for s in sessoes
                   if s.get("state") == "read"
                   and not str(s.get("remoteAddr", "")).startswith(("127.0.0.1", "[::1]")))

    async def vigiar(self):
        while True:
            try:
                assistindo_hls = time.monotonic() - self.ultimo_hls < self.HLS_OCIOSO
                self.acender(assistindo_hls or await self.espectadores() > 0)
            except Exception as e:  # MediaMTX reiniciando, privacidade...
                if self.aceso:
                    log.info("LED: sem resposta do MediaMTX (%s)", e)
                self.acender(False)
            await asyncio.sleep(1)


async def ao_iniciar(app: web.Application):
    app["http"] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=5))
    app["led"] = Led(app["http"])
    app["vigia_led"] = asyncio.create_task(app["led"].vigiar())
    app["gravador"] = Gravador()
    await app["gravador"].iniciar()


async def ao_desligar(app: web.Application):
    app["vigia_led"].cancel()
    app["led"].acender(False)
    await app["gravador"].parar()
    await app["http"].close()


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    app = web.Application()
    app.router.add_get("/", pagina)
    app.router.add_get("/cam/{resto:.*}", hls)
    app.router.add_get("/gravacoes", pagina_gravacoes)
    app.router.add_get(r"/gravacoes/{dia:\d{4}-\d{2}-\d{2}}/{hora:\d{2}-\d{2}-\d{2}}.{ext:mp4|jpg}",
                       arquivo_gravacao)
    app.router.add_get("/api/gravacoes", api_gravacoes)
    app.router.add_get("/api/estado", api_estado)
    app.router.add_post("/api/config", api_config)
    app.router.add_post("/api/privacidade", api_privacidade)
    app.router.add_post("/api/apagar", api_apagar)
    app.on_startup.append(ao_iniciar)
    app.on_cleanup.append(ao_desligar)
    web.run_app(app, host=HOST, port=PORTA, print=None, access_log_class=AccessLog)


if __name__ == "__main__":
    main()
