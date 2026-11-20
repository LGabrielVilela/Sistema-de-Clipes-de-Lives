"""Transcrição com faster-whisper (GPU quando disponível), com tempo de cada palavra.

O áudio é processado em blocos para não estourar a memória em lives de várias horas.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

import numpy as np

from .config import FFMPEG, WHISPER_CPU_MODEL, WHISPER_MODEL
from .media import NO_WINDOW

CHUNK_SECONDS = 20 * 60
_lock = threading.Lock()
_model_cache: dict = {}


class Cancelled(Exception):
    pass


def _register_cuda_dlls() -> None:
    """No Windows, as DLLs do cuBLAS/cuDNN instaladas via pip precisam ser registradas."""
    if sys.platform != "win32":
        return
    site = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    for sub in ("cublas", "cudnn", "cuda_nvrtc", "cuda_runtime"):
        b = site / sub / "bin"
        if b.is_dir():
            os.add_dll_directory(str(b))
            os.environ["PATH"] = str(b) + os.pathsep + os.environ.get("PATH", "")


_register_cuda_dlls()


def _model_is_cached(name: str) -> bool:
    try:
        from faster_whisper.utils import download_model
        download_model(name, local_files_only=True)
        return True
    except Exception:
        return False


def _load(on_progress: Callable[[float, str], None]):
    """Carrega o modelo uma única vez. Tenta GPU (int8_float16) e cai para CPU se falhar."""
    if "model" in _model_cache:
        return _model_cache["model"], _model_cache["device"]
    from faster_whisper import BatchedInferencePipeline, WhisperModel

    try:
        import ctranslate2
        has_cuda = ctranslate2.get_cuda_device_count() > 0
    except Exception:
        has_cuda = False

    if has_cuda:
        if not _model_is_cached(WHISPER_MODEL):
            on_progress(0.0, "Baixando o modelo de transcrição (só na primeira vez, cerca de 1,6 GB)")
        try:
            model = WhisperModel(WHISPER_MODEL, device="cuda", compute_type="int8_float16")
            pipe = BatchedInferencePipeline(model)
            _model_cache.update(model=pipe, device="cuda", batch=8)
            return pipe, "cuda"
        except Exception as exc:  # falta de VRAM, DLL ausente etc.
            print(f"[whisper] GPU indisponível ({exc}); usando CPU.")
    if not _model_is_cached(WHISPER_CPU_MODEL):
        on_progress(0.0, "Baixando o modelo de transcrição (só na primeira vez)")
    model = WhisperModel(WHISPER_CPU_MODEL, device="cpu", compute_type="int8")
    pipe = BatchedInferencePipeline(model)
    _model_cache.update(model=pipe, device="cpu", batch=4)
    return pipe, "cpu"


def whisper_device() -> str:
    return _model_cache.get("device", "não carregado")


def _decode_audio(media: Path, start: float, length: float) -> np.ndarray:
    """Decodifica um bloco com o ffmpeg para 16 kHz mono float32 (formato que o Whisper espera)."""
    proc = subprocess.run(
        [FFMPEG, "-v", "error", "-ss", f"{start:.2f}", "-t", f"{length:.2f}", "-i", str(media),
         "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
        capture_output=True, creationflags=NO_WINDOW,
    )
    if proc.returncode != 0:
        raise RuntimeError("Não foi possível extrair o áudio do vídeo.")
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0


# Vocabulário das lives (dica para o Whisper): ele erra menos nomes, marcas e gírias quando sabe o assunto.
# Ex.: sem a dica, "Coca Zero" saiu como "5x0".
VOCAB_PROMPT = ("Live de GTA 6 em português do Brasil, com o chat. Mano, véi, chat, sub, Pix, donate, Lucia, Jason, "
                "Vice City, Leonida, Port Gellhorn, Grassrivers, Coca-Cola, Coca Zero, easter egg, NPC, polícia, "
                "assalto, missão, gráfico, mapa, GTA V, GTA 6.")


def transcribe(media: Path, out_json: Path, language: str, total_seconds: float,
               on_progress: Callable[[float, str], None],
               cancelled: Callable[[], bool] = lambda: False, streamer: str = "") -> dict:
    with _lock:  # um job por vez na GPU
        on_progress(0.0, "Carregando modelo de transcrição")
        model, device = _load(on_progress)
        segments: list[dict] = []
        detected = language
        offset = 0.0
        total = max(total_seconds, 1.0)

        while offset < total:
            if cancelled():
                raise Cancelled()
            audio = _decode_audio(media, offset, CHUNK_SECONDS)
            if audio.size < 16000:
                break
            batch = _model_cache["batch"]
            try:
                prompt = VOCAB_PROMPT + (f" Streamer: {streamer}." if streamer else "")
                seg_iter, info = model.transcribe(audio, language=language or None, batch_size=batch,
                                                  word_timestamps=True, vad_filter=True, beam_size=5,
                                                  initial_prompt=prompt if (language or "pt").startswith("pt") else None)
                seg_list = []
                for seg in seg_iter:
                    seg_list.append(seg)
                    if cancelled():
                        raise Cancelled()
                    frac = min((offset + seg.end) / total, 0.99)
                    on_progress(frac, f"Transcrevendo ({device.upper()}) {frac*100:.0f}%")
            except RuntimeError as exc:
                if "out of memory" not in str(exc).lower() or batch <= 1:
                    raise
                _model_cache["batch"] = max(1, batch // 2)  # tenta de novo com lote menor
                continue
            detected = info.language
            for seg in seg_list:
                words = [
                    {"w": w.word.strip(), "s": round(offset + w.start, 3), "e": round(offset + w.end, 3)}
                    for w in (seg.words or []) if w.word.strip()
                ]
                segments.append({"s": round(offset + seg.start, 3), "e": round(offset + seg.end, 3),
                                 "text": seg.text.strip(), "words": words})
            offset += CHUNK_SECONDS

    data = {"language": detected, "device": device, "segments": segments}
    out_json.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data
