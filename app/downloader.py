"""Download de vídeos (VOD) e gravação de lives a partir de um link."""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from urllib.parse import urlparse

import yt_dlp
from yt_dlp.utils import download_range_func

from .config import FFMPEG
from .media import NO_WINDOW, duration, run

ProgressFn = Callable[[float, str], None]


class Cancelled(Exception):
    pass


class _Logger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def _base_opts() -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "logger": _Logger(),
        # O YouTube exige um runtime JavaScript para liberar todos os formatos;
        # usa o Deno se existir, senão o Node.
        "js_runtimes": {"deno": {}, "node": {}},
        "ffmpeg_location": str(Path(FFMPEG).parent) if Path(FFMPEG).is_file() else None,
    }


def get_info(url: str) -> dict:
    with yt_dlp.YoutubeDL({**_base_opts(), "skip_download": True}) as ydl:
        return ydl.extract_info(url, download=False)


def is_live(info: dict) -> bool:
    return bool(info.get("is_live")) or info.get("live_status") == "is_live"


# ---------------------------------------------------------------- VOD

def download_vod(url: str, job_dir: Path, on_progress: ProgressFn,
                 section: tuple[float, float] | None = None,
                 cancelled: Callable[[], bool] = lambda: False) -> Path:
    """Baixa o vídeo em até 1080p (opcionalmente só um trecho) e, quando existir, o replay do chat."""

    def hook(d):
        if cancelled():
            raise Cancelled()
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            frac = (done / total) if total else 0.0
            speed = (d.get("_speed_str") or "").strip()
            label = "Baixando trecho" if section else "Baixando vídeo"
            on_progress(min(frac, 0.99), f"{label} {frac*100:.0f}% {speed}".strip())

    opts = {
        **_base_opts(),
        # Prefere H.264 por download direto (rápido de baixar e de decodificar);
        # evita formatos "Premium" em m3u8, que são enormes.
        "format": (
            "bv*[height<=1080][vcodec^=avc1][protocol^=http]+ba[ext=m4a]/"
            "bv*[height<=1080][protocol^=http]+ba/"
            "bv*[height<=1080]+ba/b[height<=1080]/b"
        ),
        "merge_output_format": "mp4",
        "outtmpl": {"default": str(job_dir / "source.%(ext)s")},
        "progress_hooks": [hook],
        "writesubtitles": section is None,  # o chat só é útil com o vídeo inteiro
        "subtitleslangs": ["live_chat"],
        "concurrent_fragment_downloads": 4,
        # No Windows o antivírus às vezes trava o arquivo na hora de renomear
        "file_access_retries": 15,
        "retries": 10,
        "fragment_retries": 10,
    }
    if section:
        opts["download_ranges"] = download_range_func(None, [section])
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except yt_dlp.utils.DownloadError as exc:
        if isinstance(getattr(exc, "exc_info", [None, None])[1], Cancelled) or cancelled():
            raise Cancelled()
        if not opts["writesubtitles"]:
            raise
        # O replay do chat às vezes falha; tenta de novo só com o vídeo.
        opts["writesubtitles"] = False
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

    for ext in ("mp4", "mkv", "webm"):
        p = job_dir / f"source.{ext}"
        if p.exists():
            if ext != "mp4":
                mp4 = job_dir / "source.mp4"
                run([FFMPEG, "-y", "-v", "error", "-i", str(p), "-c", "copy", str(mp4)])
                p.unlink(missing_ok=True)
                return mp4
            return p
    raise RuntimeError("O download terminou, mas o arquivo de vídeo não foi encontrado.")


# ---------------------------------------------------------------- LIVE

def _live_inputs(url: str) -> list[str]:
    """Argumentos de entrada do ffmpeg para a live (vídeo e áudio podem vir separados)."""
    fmt = "bv*[height<=1080]+ba/b[height<=1080]/b"
    with yt_dlp.YoutubeDL({**_base_opts(), "format": fmt}) as ydl:
        info = ydl.extract_info(url, download=False)
    if not is_live(info):
        raise EOFError("a live terminou")
    streams = info.get("requested_formats") or [info]
    args: list[str] = []
    n = 0
    for f in streams:
        if not f.get("url"):
            continue
        headers = "".join(f"{k}: {v}\r\n" for k, v in (f.get("http_headers") or info.get("http_headers") or {}).items())
        if headers:
            args += ["-headers", headers]
        args += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "20", "-i", f["url"]]
        n += 1
    if not n:
        raise RuntimeError("Não foi possível obter o endereço da transmissão ao vivo.")
    if n > 1:
        args += ["-map", "0:v:0", "-map", "1:a:0"]
    return args


def _streamlink_exe() -> str | None:
    import shutil
    import sys
    exe = Path(sys.prefix) / "Scripts" / "streamlink.exe"
    return str(exe) if exe.exists() else shutil.which("streamlink")


def _uses_streamlink(url: str) -> bool:
    """Twitch e Kick vão pelo Streamlink, que pula os anúncios (eles travam a gravação direta)."""
    host = urlparse(url).netloc.lower()
    return bool(_streamlink_exe()) and ("twitch.tv" in host or "kick.com" in host)


def _kill(*procs) -> None:
    for p in procs:
        if p and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=10)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass


def record_live_blocks(url: str, out_dir: Path, max_seconds: int, block_seconds: int,
                       on_progress: ProgressFn, stop_event: threading.Event,
                       on_block: Callable[[Path, int], None]) -> int:
    """Grava a live em blocos contínuos. Cada bloco pronto é entregue a `on_block` enquanto a
    gravação continua. Reconecta sozinho se o sinal cair ou travar. Retorna quantos blocos saíram."""
    out_dir.mkdir(parents=True, exist_ok=True)
    delivered: set[str] = set()
    next_index = 0
    failures = 0
    recorded = 0.0  # segundos de vídeo realmente gravados nas sessões anteriores
    # sem avançar o vídeo por esse tempo = travou. Twitch/Kick têm intervalos de anúncio
    # (que o Streamlink pula) de até ~3 min, então a tolerância lá é maior.
    STALL = 240 if _uses_streamlink(url) else 60

    def deliver(final: bool):
        segs = sorted(out_dir.glob("bloco_*.ts"))
        ready = segs if final else segs[:-1]  # o último ainda está sendo gravado
        for seg in ready:
            if seg.name in delivered:
                continue
            delivered.add(seg.name)
            if seg.stat().st_size > 200_000:
                on_block(seg, int(seg.stem.split("_")[1]))

    while not stop_event.is_set():
        remaining = max_seconds - recorded
        if remaining < 20:
            break
        source_proc = None
        try:
            if _uses_streamlink(url):
                # confirma que ainda está ao vivo antes de abrir o Streamlink
                if not is_live(get_info(url)):
                    break
                source_proc = subprocess.Popen(
                    [_streamlink_exe(), "--stdout", "--stream-segment-threads", "2",
                     "--retry-open", "3", "--hls-live-edge", "3", url, "1080p60,1080p,best"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
                inputs = ["-i", "pipe:0"]
            else:
                inputs = _live_inputs(url)
        except EOFError:
            break
        except Exception:
            failures += 1
            if failures > 5:
                raise
            on_progress(min(recorded / max_seconds, 0.99), "Reconectando à live…")
            stop_event.wait(10)
            continue

        cmd = [FFMPEG, "-y", "-v", "error", "-progress", "pipe:1", "-nostats", *inputs,
               "-t", str(int(remaining)), "-c", "copy", "-map", "0:v:0?", "-map", "0:a:0?"]
        if "-map" in inputs:  # YouTube: vídeo e áudio em entradas separadas, mapeadas em _live_inputs
            cmd = [FFMPEG, "-y", "-v", "error", "-progress", "pipe:1", "-nostats", *inputs,
                   "-t", str(int(remaining)), "-c", "copy"]
        cmd += ["-f", "segment", "-segment_time", str(block_seconds), "-segment_format", "mpegts",
                "-segment_start_number", str(next_index), "-reset_timestamps", "1",
                str(out_dir / "bloco_%04d.ts")]
        proc = subprocess.Popen(cmd, stdin=source_proc.stdout if source_proc else subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        if source_proc:
            source_proc.stdout.close()  # o ffmpeg é o único leitor do pipe
        state = {"t": 0.0, "changed": time.time(), "stalled": False}

        def watchdog(p=proc, sp=source_proc):
            while p.poll() is None:
                if stop_event.wait(2):
                    if sp:          # Streamlink: encerrar a fonte faz o ffmpeg fechar o bloco direito
                        _kill(sp)
                    else:
                        try:
                            p.stdin.write("q")
                            p.stdin.flush()
                        except Exception:
                            pass
                    time.sleep(15)
                    _kill(p)
                    return
                if time.time() - state["changed"] > STALL:
                    state["stalled"] = True
                    _kill(sp, p)
                    return

        threading.Thread(target=watchdog, daemon=True).start()
        last_check = 0.0
        for line in proc.stdout:
            if not line.startswith("out_time_us="):
                continue
            try:
                t = int(line.split("=", 1)[1]) / 1_000_000
            except ValueError:
                continue
            if t > state["t"] + 0.5:
                state["t"], state["changed"] = t, time.time()
            total = recorded + state["t"]
            hh, rem = divmod(int(total), 3600)
            mm, ss = divmod(rem, 60)
            on_progress(min(total / max_seconds, 0.99),
                        f"Gravando live {hh:d}:{mm:02d}:{ss:02d} — {len(delivered)} bloco(s) enviado(s) para corte")
            if time.time() - last_check > 5:
                last_check = time.time()
                deliver(final=False)
        proc.wait()
        _kill(source_proc)
        recorded += state["t"]

        segs = sorted(out_dir.glob("bloco_*.ts"))
        if segs:
            next_index = int(segs[-1].stem.split("_")[1]) + 1
        deliver(final=True)
        if stop_event.is_set():
            break
        if state["t"] > 60:
            failures = 0
        else:
            failures += 1
            if failures > 5:
                if not delivered:
                    err = (proc.stderr.read() or "").strip().splitlines()[-4:]
                    raise RuntimeError("A gravação da live falhou.\n" + "\n".join(err))
                break
        msg = "A gravação travou; reconectando…" if state["stalled"] else "Sinal caiu; reconectando…"
        on_progress(min(recorded / max_seconds, 0.99), msg)
        stop_event.wait(5)

    deliver(final=True)
    return len(delivered)


def build_block_source(block: Path, previous: Path | None, dest: Path, overlap: int = 45) -> Path:
    """Junta o fim do bloco anterior com o bloco atual, para não perder momentos na emenda."""
    parts = []
    tmp_tail = dest.parent / "_tail.ts"
    if previous and previous.exists():
        try:
            prev_dur = duration(previous)
            run([FFMPEG, "-y", "-v", "error", "-ss", f"{max(prev_dur - overlap, 0):.2f}", "-i", str(previous),
                 "-c", "copy", "-f", "mpegts", str(tmp_tail)])
            parts.append(tmp_tail)
        except Exception:
            pass
    parts.append(block)
    src = "concat:" + "|".join(str(p) for p in parts)
    run([FFMPEG, "-y", "-v", "error", "-i", src, "-c", "copy", "-bsf:a", "aac_adtstoasc",
         "-movflags", "+faststart", str(dest)])
    tmp_tail.unlink(missing_ok=True)
    return dest
