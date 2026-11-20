"""Sinais da plateia durante a live: chat e clips da Twitch.

- Chat: leitura anônima pelo IRC oficial da Twitch (irc.chat.twitch.tv, usuário "justinfan", só leitura).
  Cada mensagem vira uma linha "horário<TAB>texto" no arquivo chat.tsv da sessão.
- Clips: a API oficial (Helix /clips) diz quando os espectadores clipparam um momento. Quem assiste clippa o que
  achou engraçado ou épico, então é o sinal mais forte de momento que viraliza.

Na Kick não há leitura de chat por API oficial (só webhook, que exige endereço público): lá valem áudio e fala.
"""
from __future__ import annotations

import random
import re
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import requests

# mensagens de risada/hype valem mais que conversa comum
HYPE = re.compile(r"(kkk|haha|rsrs|lul|kekw|omegalul|lmao|pog|clip|😂|🤣|💀|^w+$)", re.IGNORECASE)
BOTS = {"streamelements", "nightbot", "moobot", "fossabot", "wizebot", "streamlabs", "sery_bot", "pokemoncommunitygame"}
CHAT_LAG = 3  # a plateia reage alguns segundos depois do momento


def twitch_handle(url: str) -> str | None:
    """Canal da Twitch a partir do link da live (twitch.tv/canal)."""
    u = urlparse(url if "//" in url else "https://" + url)
    if "twitch.tv" not in u.netloc:
        return None
    parts = [p for p in u.path.split("/") if p]
    return parts[0].lower() if parts and parts[0] not in ("videos", "directory") else None


class TwitchChat:
    """Grava o chat de um canal enquanto a sessão estiver ativa. Reconecta sozinho se cair."""

    def __init__(self, handle: str, out: Path, stop: threading.Event) -> None:
        self.handle, self.out, self.stop = handle.lower(), out, stop
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while not self.stop.is_set():
            try:
                self._session()
            except Exception as exc:
                print(f"[chat] {self.handle}: {exc}")
            self.stop.wait(10)

    def _session(self) -> None:
        s = socket.create_connection(("irc.chat.twitch.tv", 6667), timeout=30)
        try:
            s.sendall(f"NICK justinfan{random.randint(10000, 99999)}\r\nJOIN #{self.handle}\r\n".encode())
            buf = b""
            with self.out.open("a", encoding="utf-8") as fh:
                while not self.stop.is_set():
                    try:
                        data = s.recv(8192)
                    except socket.timeout:
                        s.sendall(b"PING :tmi.twitch.tv\r\n")
                        continue
                    if not data:
                        return
                    buf += data
                    *lines, buf = buf.split(b"\r\n")
                    now = time.time()
                    for raw in lines:
                        line = raw.decode("utf-8", "replace")
                        if line.startswith("PING"):
                            s.sendall(b"PONG :tmi.twitch.tv\r\n")
                        elif " PRIVMSG " in line:
                            user = line[1:].split("!", 1)[0].lower()
                            text = line.split(" :", 1)[1] if " :" in line else ""
                            if text.startswith("!") or user in BOTS or user.endswith("bot"):
                                continue  # comandos e bots não são reação da plateia
                            fh.write(f"{now:.1f}\t{text.replace(chr(9), ' ')[:200]}\n")
                    fh.flush()
        finally:
            s.close()


def chat_curve(chat_file: Path, wall_start: float, seconds: int) -> np.ndarray | None:
    """Mensagens por segundo do trecho [wall_start, wall_start + seconds], com peso maior para risada/hype."""
    if not chat_file or not chat_file.exists():
        return None
    counts = np.zeros(max(seconds, 1), dtype=np.float32)
    with chat_file.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            t, _, text = line.partition("\t")
            try:
                sec = int(float(t) - wall_start) - CHAT_LAG
            except ValueError:
                continue
            if 0 <= sec < len(counts):
                counts[sec] += 2.0 if HYPE.search(text) else 1.0
    return counts if counts.sum() >= 10 else None


def chat_baseline(chat_file: Path) -> float:
    """Mensagens (com peso) por segundo normais da sessão inteira, para saber o que é "acima do normal"."""
    if not chat_file or not chat_file.exists():
        return 0.0
    first = last = None
    total = 0.0
    with chat_file.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            t, _, text = line.partition("\t")
            try:
                t = float(t)
            except ValueError:
                continue
            first = t if first is None else first
            last = t
            total += 2.0 if HYPE.search(text) else 1.0
    if first is None or last - first < 120:
        return 0.0
    return total / (last - first)


def twitch_clips(handle: str, wall_start: float, seconds: float) -> list[dict]:
    """Clips feitos pelos espectadores no trecho. Cada clip vira {"t": segundo do trecho, "views": n}."""
    from .monitor import twitch_token
    from .store import get_secret
    if not twitch_token.available():
        return []
    headers = {"Client-Id": get_secret("TWITCH_CLIENT_ID"), "Authorization": f"Bearer {twitch_token.get()}"}
    r = requests.get("https://api.twitch.tv/helix/users", params={"login": handle}, headers=headers, timeout=20)
    r.raise_for_status()
    users = r.json().get("data") or []
    if not users:
        return []
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = requests.get("https://api.twitch.tv/helix/clips", headers=headers, timeout=20,
                     params={"broadcaster_id": users[0]["id"], "started_at": iso(wall_start),
                             "ended_at": iso(wall_start + seconds + 120), "first": 100})
    r.raise_for_status()
    out = []
    for c in r.json().get("data") or []:
        created = datetime.fromisoformat(c["created_at"].replace("Z", "+00:00")).timestamp()
        # o clip pega os segundos ANTES de ser criado; o momento fica perto do meio dele
        t = created - wall_start - float(c.get("duration") or 30) / 2
        if 0 <= t <= seconds:
            out.append({"t": round(t, 1), "views": int(c.get("view_count") or 0), "title": c.get("title", "")})
    return out


def clips_curve(clips: list[dict], seconds: int) -> np.ndarray:
    """Cada clip soma um "morro" de ~30 s em volta do momento clippado (mais views, morro mais alto)."""
    curve = np.zeros(max(seconds, 1), dtype=np.float32)
    for c in clips:
        a, b = max(0, int(c["t"]) - 18), min(len(curve), int(c["t"]) + 12)
        curve[a:b] += 1.0 + min(2.0, np.log10(1 + c["views"]) / 2)
    return curve
