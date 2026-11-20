"""Sinais de "momento quente": energia do áudio e atividade do chat."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np

from .config import FFMPEG
from .media import NO_WINDOW

SR = 8000  # taxa baixa basta para medir volume


def audio_energy(media: Path) -> np.ndarray:
    """Volume (dB) por segundo do vídeo inteiro."""
    cmd = [FFMPEG, "-v", "error", "-i", str(media), "-vn", "-ac", "1", "-ar", str(SR),
           "-f", "s16le", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
    values: list[float] = []
    buf = b""
    chunk = SR * 2  # 1 segundo de amostras de 16 bits
    while True:
        data = proc.stdout.read(chunk * 60)
        if not data:
            break
        buf += data
        n = len(buf) // chunk
        if n:
            arr = np.frombuffer(buf[: n * chunk], dtype=np.int16).astype(np.float32).reshape(n, SR)
            rms = np.sqrt(np.mean(arr ** 2, axis=1)) + 1e-6
            values.extend((20 * np.log10(rms / 32768)).tolist())
            buf = buf[n * chunk:]
    proc.wait()
    return np.array(values, dtype=np.float32)


def chat_activity(job_dir: Path, total_seconds: int) -> np.ndarray | None:
    """Mensagens de chat por segundo (replay de chat do YouTube, quando existir)."""
    files = list(job_dir.glob("*.live_chat.json"))
    if not files:
        return None
    counts = np.zeros(max(total_seconds, 1), dtype=np.float32)
    with files[0].open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                item = json.loads(line)
                off = item["replayChatItemAction"]["videoOffsetTimeMsec"]
                sec = int(int(off) / 1000)
            except (KeyError, ValueError, json.JSONDecodeError):
                continue
            if 0 <= sec < len(counts):
                counts[sec] += 1
    return counts if counts.sum() > 0 else None


def _zscore(x: np.ndarray, window: int = 31) -> np.ndarray:
    """Destaque relativo: o quanto cada segundo está acima do normal da vizinhança."""
    if len(x) == 0:
        return x
    k = np.ones(window) / window
    smooth = np.convolve(x, np.ones(5) / 5, mode="same")
    base = np.convolve(smooth, k, mode="same")
    std = np.std(smooth) + 1e-6
    return (smooth - base) / std


def excitement_curve(energy: np.ndarray, chat: np.ndarray | None, clips: np.ndarray | None = None) -> np.ndarray:
    """Volume + chat (peso maior) + clips da plateia (peso maior ainda: alguém achou o momento bom o
    bastante para clippar)."""
    score = _zscore(energy)
    if chat is not None:
        n = min(len(score), len(chat))
        chat_s = np.convolve(chat[:n], np.ones(10) / 10, mode="same")
        score = score[:n] + 1.5 * _zscore(chat_s)
    if clips is not None and clips.any():
        n = min(len(score), len(clips))
        score = score[:n] + 1.5 * clips[:n]
    return score


def top_peaks(curve: np.ndarray, count: int = 25, min_gap: int = 45) -> list[dict]:
    """Os segundos mais 'quentes', espaçados entre si."""
    order = np.argsort(curve)[::-1]
    peaks: list[int] = []
    for idx in order:
        if curve[idx] <= 0.5:
            break
        if all(abs(int(idx) - p) >= min_gap for p in peaks):
            peaks.append(int(idx))
        if len(peaks) >= count:
            break
    return [{"t": p, "score": round(float(curve[p]), 2)} for p in sorted(peaks)]


def timeline_points(curve: np.ndarray, points: int = 240) -> list[float]:
    """Versão reduzida da curva (0..1) para desenhar no painel."""
    if len(curve) == 0:
        return []
    bins = np.array_split(curve, min(points, len(curve)))
    vals = np.array([b.max() for b in bins])
    lo, hi = np.percentile(vals, 5), vals.max()
    return np.clip((vals - lo) / (hi - lo + 1e-6), 0, 1).round(3).tolist()
