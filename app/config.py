"""Configurações gerais do Sistema de Cortes."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
JOBS_DIR = DATA_DIR / "jobs"
WEB_DIR = ROOT / "web"
JOBS_DIR.mkdir(parents=True, exist_ok=True)


def _find_bin(name: str) -> str:
    """Localiza o ffmpeg/ffprobe mesmo que o PATH do terminal ainda não tenha sido recarregado."""
    found = shutil.which(name)
    if found:
        return found
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / f"{name}.exe",
    ]
    pkg_root = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    if pkg_root.exists():
        candidates += list(pkg_root.glob(f"Gyan.FFmpeg*/**/bin/{name}.exe"))
    for c in candidates:
        if c.exists():
            return str(c)
    return name


FFMPEG = os.environ.get("FFMPEG_BIN") or _find_bin("ffmpeg")
FFPROBE = os.environ.get("FFPROBE_BIN") or _find_bin("ffprobe")
# Algumas checagens do yt-dlp só olham o PATH; garante que o ffmpeg encontrado esteja nele
# (importante logo após instalar o ffmpeg, quando o PATH do Windows ainda não foi recarregado).
if Path(FFMPEG).is_file():
    os.environ["PATH"] = str(Path(FFMPEG).resolve().parent) + os.pathsep + os.environ.get("PATH", "")

# Transcrição (faster-whisper). large-v3-turbo é o melhor custo/benefício para português
# e cabe em GPUs de 4 GB usando int8_float16.
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "large-v3-turbo")
WHISPER_CPU_MODEL = os.environ.get("WHISPER_CPU_MODEL", "small")
WHISPER_LANGUAGE = os.environ.get("WHISPER_LANGUAGE", "pt")

# IA que escolhe os momentos
# Haiku 5.5: o Claude mais barato (centavos por live). Troque por claude-opus-5-5 se quiser o máximo.
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))


def json_default(o):
    """Permite salvar números do NumPy (float32, int64...) que escaparem para os dados."""
    if hasattr(o, "item"):
        return o.item()
    if hasattr(o, "tolist"):
        return o.tolist()
    raise TypeError(f"{type(o).__name__} não pode ser salvo")
