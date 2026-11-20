"""Utilidades de ffmpeg/ffprobe."""
from __future__ import annotations

import json
import subprocess
from functools import lru_cache
from pathlib import Path

from .config import FFMPEG, FFPROBE

# Evita abrir janelas de console no Windows
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run(cmd: list[str], cwd: Path | None = None, timeout: float | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout, creationflags=NO_WINDOW,
    )
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-12:])
        raise RuntimeError(f"Falha ao executar {Path(cmd[0]).name}:\n{tail}")
    return proc


def probe(path: Path) -> dict:
    out = run([FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)])
    return json.loads(out.stdout)


def duration(path: Path) -> float:
    return float(probe(path)["format"]["duration"])


def video_size(path: Path) -> tuple[int, int]:
    for s in probe(path)["streams"]:
        if s.get("codec_type") == "video":
            return int(s["width"]), int(s["height"])
    raise RuntimeError("O arquivo não tem faixa de vídeo.")


@lru_cache(maxsize=1)
def video_encoder_args() -> tuple[str, ...]:
    """Usa a NVENC da placa NVIDIA quando disponível (bem mais rápido); senão libx264."""
    try:
        run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.2",
             "-c:v", "h264_nvenc", "-f", "null", "-"], timeout=30)
        return ("-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "21", "-b:v", "0",
                "-profile:v", "high")
    except Exception:
        return ("-c:v", "libx264", "-preset", "medium", "-crf", "20", "-profile:v", "high")


def ffmpeg_ok() -> bool:
    try:
        run([FFMPEG, "-version"], timeout=15)
        return True
    except Exception:
        return False


def av_offset(path: Path) -> float:
    """Quanto o vídeo começa depois do áudio no arquivo (s). A transcrição mede os tempos pelo áudio,
    e a legenda é queimada na linha do tempo do vídeo; essa diferença precisa ser compensada."""
    try:
        st = {s["codec_type"]: float(s.get("start_time") or 0) for s in probe(path)["streams"]
              if s.get("codec_type") in ("video", "audio")}
        return st.get("video", 0.0) - st.get("audio", 0.0) if len(st) == 2 else 0.0
    except Exception:
        return 0.0
