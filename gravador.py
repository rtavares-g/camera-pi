"""
Gravacao por movimento (estilo DVR).

O MediaMTX grava a camera sem parar em segmentos curtos (fMP4 de 5 s) num
buffer em RAM (/dev/shm). Este modulo:

- le a camera pelo RTSP local decodificando so os quadros-chave (1 por
  segundo, reduzidos para 80x45 em cinza - quase nenhuma CPU) e compara cada
  quadro com o anterior para detectar movimento;
- enquanto nao ha movimento, apaga os segmentos antigos do buffer, guardando
  so o ultimo completo (pre-gravacao);
- quando ha movimento, abre um evento e vai movendo os segmentos para o
  disco; alguns segundos depois do ultimo movimento junta tudo num .mp4
  (sem recodificar) com uma miniatura do momento de maior movimento;
- apaga eventos com mais de RETENCAO_DIAS dias (ou os mais antigos, se o
  disco estiver ficando cheio).

Layout no disco:  GRAVACOES_DIR/AAAA-MM-DD/HH-MM-SS.{mp4,jpg,json}
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("camera-pi.gravador")

RTSP = os.environ.get("MEDIAMTX_RTSP", "rtsp://127.0.0.1:8554/cam")
BUFFER_DIR = Path(os.environ.get("BUFFER_DIR", "/dev/shm/camera-pi/cam"))
GRAVACOES_DIR = Path(os.environ.get("GRAVACOES_DIR", str(Path.home() / "camera-gravacoes")))
RETENCAO_DIAS = float(os.environ.get("RETENCAO_DIAS", "5"))
# Segundos de gravacao depois do ultimo movimento
POS_GRAVACAO = float(os.environ.get("POS_GRAVACAO", "10"))
# Eventos longos sao quebrados em arquivos deste tamanho (segundos)
MAX_EVENTO = float(os.environ.get("MAX_EVENTO", "600"))
# Abaixo disso de espaco livre, apaga os eventos mais antigos
MIN_LIVRE = int(float(os.environ.get("MIN_LIVRE_GB", "5")) * 1024**3)

LARGURA, ALTURA = 80, 45
PIXELS = LARGURA * ALTURA
# Diferenca de brilho (0-255) para um pixel contar como "mudou"
DELTA_PIXEL = 18
# Fracao de pixels que precisa mudar, por sensibilidade
SENSIBILIDADES = {"baixa": 0.04, "media": 0.015, "alta": 0.006}
# Quadros ignorados quando a camera (re)liga: a exposicao automatica ainda
# esta se ajustando e o brilho muda o quadro inteiro.
AQUECIMENTO = 6

RE_SEGMENTO = re.compile(r"(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})-(\d+)\.mp4$")
RE_DIA = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RE_EVENTO = re.compile(r"^\d{2}-\d{2}-\d{2}$")


def inicio_segmento(caminho: Path) -> float | None:
    m = RE_SEGMENTO.search(caminho.name)
    if not m:
        return None
    dt = datetime.strptime(m.group(1), "%Y-%m-%d_%H-%M-%S")
    return dt.timestamp() + int(m.group(2)) / 10 ** len(m.group(2))


@dataclass
class Evento:
    pasta: Path
    inicio: float
    ultimo_movimento: float
    pico: float = 0.0
    momento_pico: float = 0.0
    segmentos: list[Path] = field(default_factory=list)


class Gravador:
    def __init__(self) -> None:
        self.parcial = GRAVACOES_DIR / ".parcial"
        self.arquivo_config = GRAVACOES_DIR / "config.json"
        self.config = {"ativo": True, "sensibilidade": "media"}
        self.evento: Evento | None = None
        self.movimento = 0.0          # fracao de pixels mudados no ultimo quadro
        self.ultimo_quadro_em = 0.0   # para saber se o detector esta vivo
        self.anterior: bytes | None = None
        self.aquecendo = 0
        self.tarefas: list[asyncio.Task] = []
        self.finalizando: set[asyncio.Task] = set()

    # ---------- configuracao ----------

    def carregar_config(self) -> None:
        try:
            self.config.update(json.loads(self.arquivo_config.read_text()))
        except (OSError, ValueError):
            pass
        # Privacidade: bloqueia detector e gravação
        if self.config.get("privacidade"):
            self.config["ativo"] = False

    def salvar_config(self, novos: dict) -> dict:
        if "ativo" in novos:
            self.config["ativo"] = bool(novos["ativo"])
        if novos.get("sensibilidade") in SENSIBILIDADES:
            self.config["sensibilidade"] = novos["sensibilidade"]
        if "privacidade" in novos:
            self.config["privacidade"] = bool(novos["privacidade"])
            # Privacidade desliga tudo
            if self.config["privacidade"]:
                self.config["ativo"] = False
                if self.evento:
                    self._fechar_evento()
        self.arquivo_config.write_text(json.dumps(self.config))
        log.info("Config: %s", self.config)
        return self.config

    # ---------- ciclo de vida ----------

    async def iniciar(self) -> None:
        GRAVACOES_DIR.mkdir(parents=True, exist_ok=True)
        self.parcial.mkdir(exist_ok=True)
        self.carregar_config()
        # Eventos que ficaram pela metade (queda de energia, restart)
        for pasta in sorted(self.parcial.iterdir()):
            self._agendar_finalizacao(pasta, {})
        self.tarefas = [
            asyncio.create_task(self._detector()),
            asyncio.create_task(self._ciclo()),
            asyncio.create_task(self._limpeza()),
        ]

    async def parar(self) -> None:
        for t in self.tarefas:
            t.cancel()
        if self.evento:
            self._fechar_evento()
        if self.finalizando:
            await asyncio.wait(self.finalizando, timeout=30)

    # ---------- deteccao ----------

    async def _detector(self) -> None:
        while True:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-threads", "1",
                "-rtsp_transport", "tcp", "-skip_frame", "nokey", "-i", RTSP,
                "-an", "-vf", f"scale={LARGURA}:{ALTURA}:flags=area,format=gray",
                "-fps_mode", "passthrough", "-f", "rawvideo", "-",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            log.info("Detector de movimento ligado")
            self.anterior = None
            self.aquecendo = AQUECIMENTO
            try:
                assert proc.stdout
                while True:
                    quadro = await proc.stdout.readexactly(PIXELS)
                    self._analisar(quadro)
            except asyncio.IncompleteReadError:
                log.warning("Detector: stream acabou, religando em 5 s")
            finally:
                if proc.returncode is None:
                    proc.kill()
                await proc.wait()
            await asyncio.sleep(5)

    def _analisar(self, quadro: bytes) -> None:
        agora = time.time()
        self.ultimo_quadro_em = agora
        anterior, self.anterior = self.anterior, quadro
        if anterior is None:
            return
        if self.aquecendo:
            self.aquecendo -= 1
            return
        mudados = sum(1 for a, b in zip(quadro, anterior) if abs(a - b) > DELTA_PIXEL)
        self.movimento = mudados / PIXELS
        if not self.config["ativo"]:
            return
        if self.movimento < SENSIBILIDADES[self.config["sensibilidade"]]:
            return
        if self.evento is None:
            self._abrir_evento(agora)
        ev = self.evento
        assert ev
        ev.ultimo_movimento = agora
        if self.movimento > ev.pico:
            ev.pico, ev.momento_pico = self.movimento, agora

    # ---------- eventos ----------

    def _segmentos_completos(self) -> list[Path]:
        """Segmentos do buffer em ordem; o mais novo ainda esta sendo gravado."""
        try:
            todos = sorted(p for p in BUFFER_DIR.iterdir() if RE_SEGMENTO.search(p.name))
        except FileNotFoundError:
            return []
        return todos[:-1]

    def _abrir_evento(self, agora: float) -> None:
        pasta = self.parcial / datetime.fromtimestamp(agora).strftime("%Y-%m-%d_%H-%M-%S")
        pasta.mkdir(exist_ok=True)
        self.evento = Evento(pasta=pasta, inicio=agora, ultimo_movimento=agora)
        log.info("Movimento (%.1f%%): gravando", self.movimento * 100)
        # Pre-gravacao: o ultimo segmento completo que sobrou no buffer
        self._recolher(self.evento)

    def _recolher(self, ev: Evento) -> None:
        """Move os segmentos completos do buffer para a pasta do evento."""
        for seg in self._segmentos_completos():
            destino = ev.pasta / seg.name
            try:
                shutil.move(str(seg), destino)
            except OSError as e:
                log.warning("Nao consegui mover %s: %s", seg.name, e)
                continue
            ev.segmentos.append(destino)

    def _fechar_evento(self) -> None:
        ev = self.evento
        assert ev
        self.evento = None
        self._recolher(ev)
        meta = {"pico": round(ev.pico, 4), "momento_pico": ev.momento_pico}
        (ev.pasta / "meta.json").write_text(json.dumps(meta))
        self._agendar_finalizacao(ev.pasta, meta)

    async def _ciclo(self) -> None:
        while True:
            await asyncio.sleep(1)
            try:
                self._passo()
            except Exception:
                log.exception("Erro no ciclo de gravacao")

    def _passo(self) -> None:
        agora = time.time()
        ev = self.evento
        if ev is None:
            # Sem evento: mantem so o ultimo segmento completo (pre-gravacao)
            for seg in self._segmentos_completos()[:-1]:
                seg.unlink(missing_ok=True)
            return
        self._recolher(ev)
        # Fecha quando o pos-gravacao ja caiu num segmento completo
        # (segmentos de ~5 s, entao espera um pouco mais que POS_GRAVACAO).
        if agora - ev.ultimo_movimento > POS_GRAVACAO + 6 or not self.config["ativo"]:
            log.info("Fim do movimento: evento de %.0f s", agora - ev.inicio)
            self._fechar_evento()
        elif agora - ev.inicio > MAX_EVENTO:
            log.info("Evento passou de %.0f s, dividindo", MAX_EVENTO)
            ultimo = ev.ultimo_movimento
            self._fechar_evento()
            self._abrir_evento(agora)
            assert self.evento
            self.evento.ultimo_movimento = ultimo

    def _agendar_finalizacao(self, pasta: Path, meta: dict) -> None:
        t = asyncio.create_task(self._finalizar(pasta, meta))
        self.finalizando.add(t)
        t.add_done_callback(self.finalizando.discard)

    async def _finalizar(self, pasta: Path, meta: dict) -> None:
        """Junta os segmentos do evento num .mp4 com miniatura e metadados."""
        if not meta:
            try:
                meta = json.loads((pasta / "meta.json").read_text())
            except (OSError, ValueError):
                meta = {}
        segmentos = sorted(p for p in pasta.iterdir() if RE_SEGMENTO.search(p.name))
        if not segmentos:
            shutil.rmtree(pasta, ignore_errors=True)
            return
        inicio = inicio_segmento(segmentos[0]) or pasta.stat().st_mtime
        dt = datetime.fromtimestamp(inicio)
        dia = GRAVACOES_DIR / dt.strftime("%Y-%m-%d")
        dia.mkdir(exist_ok=True)
        base = dia / dt.strftime("%H-%M-%S")
        lista = pasta / "lista.txt"
        lista.write_text("".join(f"file '{s.name}'\n" for s in segmentos))
        ok = await _rodar("ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                          "-f", "concat", "-safe", "0", "-i", str(lista),
                          "-c", "copy", "-movflags", "+faststart", f"{base}.mp4")
        if not ok:
            log.error("Falha ao juntar o evento %s (segmentos mantidos)", pasta.name)
            return
        duracao = await _duracao(Path(f"{base}.mp4"))
        pos_pico = min(max(meta.get("momento_pico", 0) - inicio, 0.0), max(duracao - 0.5, 0))
        await _rodar("ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                     "-ss", f"{pos_pico:.1f}", "-i", f"{base}.mp4", "-frames:v", "1",
                     "-vf", "scale=320:-2", "-q:v", "5", f"{base}.jpg")
        Path(f"{base}.json").write_text(json.dumps({
            "inicio": inicio,
            "duracao": round(duracao, 1),
            "pico": meta.get("pico", 0),
        }))
        shutil.rmtree(pasta, ignore_errors=True)
        log.info("Evento salvo: %s (%.0f s)", base.relative_to(GRAVACOES_DIR), duracao)

    # ---------- retencao ----------

    async def _limpeza(self) -> None:
        while True:
            try:
                self.aplicar_retencao()
            except Exception:
                log.exception("Erro na limpeza")
            await asyncio.sleep(600)

    def aplicar_retencao(self) -> None:
        limite = datetime.now() - timedelta(days=RETENCAO_DIAS)
        eventos = [e for d in self.eventos() for e in d["eventos"]]
        for e in eventos:
            if e["inicio"] < limite.timestamp():
                log.info("Retencao: apagando %s", e["id"])
                self.apagar(e["id"])
        # Disco ficando cheio: apaga os mais antigos ate sobrar MIN_LIVRE
        eventos = sorted((e for d in self.eventos() for e in d["eventos"]),
                         key=lambda e: e["inicio"])
        while eventos and shutil.disk_usage(GRAVACOES_DIR).free < MIN_LIVRE:
            e = eventos.pop(0)
            log.warning("Disco cheio: apagando %s", e["id"])
            self.apagar(e["id"])

    # ---------- consulta / gerencia (usado pela API) ----------

    def eventos(self) -> list[dict]:
        """Dias (mais recente primeiro) com seus eventos (mais recente primeiro)."""
        dias = []
        for pasta in sorted(GRAVACOES_DIR.iterdir(), reverse=True):
            if not (pasta.is_dir() and RE_DIA.match(pasta.name)):
                continue
            lista = []
            for mp4 in sorted(pasta.glob("*.mp4"), reverse=True):
                if not RE_EVENTO.match(mp4.stem):
                    continue
                try:
                    meta = json.loads(mp4.with_suffix(".json").read_text())
                except (OSError, ValueError):
                    meta = {"inicio": datetime.strptime(
                        f"{pasta.name} {mp4.stem}", "%Y-%m-%d %H-%M-%S").timestamp(),
                        "duracao": 0, "pico": 0}
                lista.append({
                    "id": f"{pasta.name}/{mp4.stem}",
                    "inicio": meta["inicio"],
                    "duracao": meta["duracao"],
                    "pico": meta["pico"],
                    "tamanho": mp4.stat().st_size,
                })
            if lista:
                dias.append({"dia": pasta.name, "eventos": lista})
            else:
                shutil.rmtree(pasta, ignore_errors=True)
        return dias

    @staticmethod
    def caminho(id_evento: str, extensao: str) -> Path | None:
        """Valida 'AAAA-MM-DD/HH-MM-SS' e devolve o arquivo (ou None)."""
        dia, _, hora = id_evento.partition("/")
        if not (RE_DIA.match(dia) and RE_EVENTO.match(hora)) or extensao not in ("mp4", "jpg"):
            return None
        p = GRAVACOES_DIR / dia / f"{hora}.{extensao}"
        return p if p.is_file() else None

    def apagar(self, id_evento: str) -> bool:
        dia, _, hora = id_evento.partition("/")
        if not (RE_DIA.match(dia) and RE_EVENTO.match(hora)):
            return False
        achou = False
        for ext in ("mp4", "jpg", "json"):
            p = GRAVACOES_DIR / dia / f"{hora}.{ext}"
            if p.exists():
                p.unlink()
                achou = True
        pasta = GRAVACOES_DIR / dia
        if pasta.is_dir() and not any(pasta.iterdir()):
            pasta.rmdir()
        return achou

    def estado(self) -> dict:
        uso = shutil.disk_usage(GRAVACOES_DIR)
        ocupado = sum(p.stat().st_size for p in GRAVACOES_DIR.glob("*/*") if p.is_file())
        return {
            "config": self.config,
            "sensibilidades": list(SENSIBILIDADES),
            "detector_ok": time.time() - self.ultimo_quadro_em < 10,
            "movimento": round(self.movimento, 4),
            "limiar": SENSIBILIDADES[self.config["sensibilidade"]],
            "gravando": self.evento is not None,
            "gravando_desde": self.evento.inicio if self.evento else None,
            "processando": len(self.finalizando),
            "retencao_dias": RETENCAO_DIAS,
            "ocupado": ocupado,
            "livre": uso.free,
        }


async def _rodar(*cmd: str) -> bool:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode != 0:
        log.warning("%s falhou: %s", cmd[0], err.decode(errors="replace")[-300:])
    return proc.returncode == 0


async def _duracao(arquivo: Path) -> float:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "csv=p=0", str(arquivo),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    saida, _ = await proc.communicate()
    try:
        return float(saida.strip())
    except ValueError:
        return 0.0
