"""Vigia os streamers cadastrados e começa a gravar/cortar quando entram ao vivo jogando o jogo certo.

- Twitch: API oficial (Helix) quando há TWITCH_CLIENT_ID/SECRET; senão yt-dlp.
- Kick: API oficial quando há KICK_CLIENT_ID/SECRET; senão yt-dlp.
- YouTube: yt-dlp na página /live do canal (não gasta cota da API).
"""
from __future__ import annotations

import re
import threading
import time
import traceback
import uuid
from urllib.parse import urlparse

import requests

from . import downloader
from .store import get_config, get_secret, update_config

UA = {"User-Agent": "cortador-local/1.0"}


# ---------------------------------------------------------------- cadastro

def parse_streamer(text: str, platform: str | None = None) -> tuple[str, str]:
    """Aceita link ou nome. Retorna (plataforma, identificador)."""
    text = text.strip()
    if text.startswith(("http://", "https://")) or "." in text.split("/")[0]:
        u = urlparse(text if "://" in text else "https://" + text)
        host = u.netloc.lower()
        parts = [p for p in u.path.split("/") if p]
        if "twitch.tv" in host and parts:
            return "twitch", parts[0].lower()
        if "kick.com" in host and parts:
            return "kick", parts[0].lower()
        if "youtube.com" in host and parts:
            if parts[0].startswith("@"):
                return "youtube", parts[0]
            if parts[0] in ("channel", "c", "user") and len(parts) > 1:
                return "youtube", f"{parts[0]}/{parts[1]}"
        raise ValueError("Link não reconhecido. Use um link de canal da Twitch, Kick ou YouTube.")
    if not platform:
        raise ValueError("Escolha a plataforma do streamer.")
    handle = text.lstrip("@")
    if platform == "youtube":
        handle = "@" + handle
    return platform, handle.lower() if platform != "youtube" else handle


def channel_url(platform: str, handle: str) -> str:
    if platform == "twitch":
        return f"https://www.twitch.tv/{handle}"
    if platform == "kick":
        return f"https://kick.com/{handle}"
    if handle.startswith("@"):
        return f"https://www.youtube.com/{handle}/live"
    return f"https://www.youtube.com/{handle}/live"


def add_streamer(text: str, platform: str | None, any_game: bool) -> dict:
    plat, handle = parse_streamer(text, platform)
    cfg = get_config()
    if any(s["platform"] == plat and s["handle"].lower() == handle.lower() for s in cfg["streamers"]):
        raise ValueError("Esse streamer já está na lista.")
    s = {"id": uuid.uuid4().hex[:8], "platform": plat, "handle": handle, "name": handle.lstrip("@"),
         "any_game": bool(any_game), "enabled": True}
    update_config({"streamers": cfg["streamers"] + [s]})
    return s


def edit_streamer(sid: str, **fields) -> None:
    cfg = get_config()
    ss = [dict(s, **{k: v for k, v in fields.items() if k in ("any_game", "enabled", "name", "cam_box", "cam_fixed")})
          if s["id"] == sid else s for s in cfg["streamers"]]
    update_config({"streamers": ss})


def remove_streamer(sid: str) -> None:
    cfg = get_config()
    update_config({"streamers": [s for s in cfg["streamers"] if s["id"] != sid]})


# ---------------------------------------------------------------- APIs oficiais

class _AppToken:
    def __init__(self, url: str, id_key: str, secret_key: str):
        self.url, self.id_key, self.secret_key = url, id_key, secret_key
        self.token, self.expires = None, 0.0

    def available(self) -> bool:
        return bool(get_secret(self.id_key) and get_secret(self.secret_key))

    def get(self) -> str:
        if self.token and time.time() < self.expires - 120:
            return self.token
        r = requests.post(self.url, data={"client_id": get_secret(self.id_key),
                                          "client_secret": get_secret(self.secret_key),
                                          "grant_type": "client_credentials"},
                          headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=20)
        r.raise_for_status()
        d = r.json()
        self.token = d["access_token"]
        self.expires = time.time() + float(d.get("expires_in") or 3600)
        return self.token


twitch_token = _AppToken("https://id.twitch.tv/oauth2/token", "TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET")
kick_token = _AppToken("https://id.kick.com/oauth/token", "KICK_CLIENT_ID", "KICK_CLIENT_SECRET")


def _twitch_live(handles: list[str]) -> dict[str, dict]:
    out = {}
    for i in range(0, len(handles), 100):
        params = [("user_login", h) for h in handles[i:i + 100]] + [("first", "100")]
        r = requests.get("https://api.twitch.tv/helix/streams", params=params, timeout=20,
                         headers={"Client-Id": get_secret("TWITCH_CLIENT_ID"),
                                  "Authorization": f"Bearer {twitch_token.get()}"})
        r.raise_for_status()
        for s in r.json().get("data", []):
            if s.get("type") == "live":
                out[s["user_login"].lower()] = {"title": s.get("title", ""), "category": s.get("game_name", ""),
                                                "viewers": s.get("viewer_count", 0)}
    return out


def _kick_live(handles: list[str]) -> dict[str, dict]:
    out = {}
    for i in range(0, len(handles), 50):
        params = [("slug", h) for h in handles[i:i + 50]]
        r = requests.get("https://api.kick.com/public/v1/channels", params=params, timeout=20,
                         headers={"Authorization": f"Bearer {kick_token.get()}", **UA})
        r.raise_for_status()
        for c in r.json().get("data", []):
            st = c.get("stream") or {}
            if st.get("is_live"):
                out[c["slug"].lower()] = {"title": c.get("stream_title", ""),
                                          "category": (c.get("category") or {}).get("name", ""),
                                          "viewers": st.get("viewer_count", 0)}
    return out


def _ytdlp_live(platform: str, handle: str) -> dict | None:
    try:
        info = downloader.get_info(channel_url(platform, handle))
    except Exception:
        return None  # offline (o yt-dlp levanta erro quando não há live)
    if not downloader.is_live(info):
        return None
    cats = info.get("categories") or []
    title = info.get("title") or ""
    if platform == "twitch" and info.get("description"):
        title = info["description"]  # na Twitch o título real da live vem nesse campo
    return {"title": title, "category": ", ".join(c for c in cats if c),
            "viewers": info.get("concurrent_view_count") or 0, "url": info.get("webpage_url")}


# ---------------------------------------------------------------- vigia

class Monitor:
    def __init__(self, store) -> None:
        self.store = store  # JobStore
        self.status: dict[str, dict] = {}
        self._last_turn: dict[str, float] = self._load_turns()
        self.last_run = 0.0
        self.running = False
        self._wake = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def check_now(self) -> None:
        self._wake.set()

    @staticmethod
    def matches(info: dict, keywords: list[str]) -> bool:
        text = f"{info.get('category', '')} {info.get('title', '')}".lower()
        text = re.sub(r"\s+", " ", text)
        if re.search(r"\b(rerun|reprise)\b", text):  # reprise = conteúdo repetido
            return False
        # palavra inteira: "gta vi" não pode casar com "gta vida", nem "gta 6" com "gta 60fps"
        return any(re.search(rf"(?<!\w){re.escape(k.strip().lower())}(?!\w)", text)
                   for k in keywords if k.strip())

    def _loop(self) -> None:
        while True:
            cfg = get_config()
            if cfg["monitor"]["enabled"] and cfg["streamers"]:
                try:
                    self.running = True
                    self._tick(cfg)
                except Exception:
                    traceback.print_exc()
                finally:
                    self.running = False
                    self.last_run = time.time()
            self._wake.wait(max(1, cfg["monitor"]["interval_min"]) * 60)
            self._wake.clear()

    def _tick(self, cfg: dict) -> None:
        mon = cfg["monitor"]
        streamers = [s for s in cfg["streamers"] if s.get("enabled", True)]
        live: dict[str, dict | None] = {}
        errors: dict[str, str] = {}

        by_plat = {p: [s for s in streamers if s["platform"] == p] for p in ("twitch", "kick", "youtube")}
        for plat, api, tok in (("twitch", _twitch_live, twitch_token), ("kick", _kick_live, kick_token)):
            group = by_plat[plat]
            if group and tok.available():
                try:
                    res = api([s["handle"] for s in group])
                    for s in group:
                        live[s["id"]] = res.get(s["handle"].lower())
                    continue
                except Exception as exc:
                    for s in group:
                        errors[s["id"]] = f"API da {plat.title()} falhou ({exc}); usando método alternativo"
            for s in group:
                live[s["id"]] = _ytdlp_live(plat, s["handle"])
        for s in by_plat["youtube"]:
            live[s["id"]] = _ytdlp_live("youtube", s["handle"])

        active = self._active_sessions()
        now = time.time()
        for sid in active:  # quem está gravando agora: "gravado por último" = agora
            self._last_turn[sid] = now
        statuses, waiting = {}, []
        for s in streamers:
            info = live.get(s["id"])
            st = {"checked_at": now, "live": bool(info), "error": errors.get(s["id"])}
            if info:
                ok = s.get("any_game") or self.matches(info, mon["keywords"])
                st.update(title=info["title"], category=info["category"], viewers=info["viewers"], matched=ok)
                if active.get(s["id"]):
                    st["job_id"] = active[s["id"]]
                elif ok:
                    waiting.append((s, info))
            statuses[s["id"]] = st

        # Rodízio: com vagas cheias e gente esperando, quem já gravou um turno inteiro cede a vaga.
        # (Parar fecha o bloco em andamento e manda para corte: nada se perde.)
        turn = int(mon.get("turn_minutes", 60)) * 60
        if turn and waiting and len(active) >= mon["max_concurrent"]:
            done_turn = []
            for sid, jid in active.items():
                j = self.store.get(jid) or {}
                if j.get("auto") and not j.get("rotating") and now - j.get("created_at", now) >= turn:
                    done_turn.append((j["created_at"], sid, jid))
            for _, sid, jid in sorted(done_turn)[:len(waiting)]:
                self.store.update(jid, rotating=True)
                self.store.stop_live(jid)
                statuses[sid]["note"] = "Turno de gravação encerrado (rodízio): a vaga vai para outro streamer"
            if done_turn:  # a gravação leva uns segundos para fechar: confere de novo logo, sem esperar 3 min
                threading.Timer(40, self.check_now).start()
        self._save_turns()

        # Quem entra: mais tempo sem ser gravado primeiro (nunca gravado = primeiro de todos);
        # empate: Twitch (tem chat e clips da plateia) e mais espectadores.
        waiting.sort(key=lambda si: (self._last_turn.get(si[0]["id"], 0.0),
                                     0 if si[0]["platform"] == "twitch" else 1, -int(si[1].get("viewers") or 0)))
        for pos, (s, info) in enumerate(waiting):
            st = statuses[s["id"]]
            if len(active) >= mon["max_concurrent"]:
                st["note"] = st.get("note") or (f"Na fila do rodízio: {pos + 1}º da vez "
                                                f"(entra quando uma gravação completar o turno)")
            else:
                job = self.store.create(
                    info.get("url") or channel_url(s["platform"], s["handle"]), "live",
                    {"num_clips": mon["clips_per_block"], "min_len": mon["min_len"],
                     "max_len": mon["max_len"], "layout": mon["layout"],
                     "cold_open": mon.get("cold_open", True), "loop_end": mon.get("loop_end", True), "min_score": mon.get("min_score", 55),
                     "block_minutes": mon["block_minutes"], "live_minutes": int(mon["max_hours"] * 60)},
                    streamer_id=s["id"], streamer=s["name"], platform=s["platform"],
                    source_channel=channel_url(s["platform"], s["handle"]).removesuffix("/live"),
                    auto=True)
                active[s["id"]] = job["id"]
                self._last_turn[s["id"]] = now
                st["job_id"] = job["id"]
                st["note"] = "Gravação iniciada automaticamente"
        self.status.update(statuses)
        self._save_turns()

    # quando cada streamer foi gravado pela última vez (para o rodízio), guardado em disco
    def _load_turns(self) -> dict[str, float]:
        import json
        from .config import DATA_DIR
        try:
            return {k: float(v) for k, v in json.loads((DATA_DIR / "monitor_turns.json").read_text()).items()}
        except Exception:
            return {}

    def _save_turns(self) -> None:
        import json
        from .config import DATA_DIR
        try:
            (DATA_DIR / "monitor_turns.json").write_text(json.dumps(self._last_turn))
        except Exception:
            traceback.print_exc()

    def _active_sessions(self) -> dict[str, str]:
        out = {}
        for j in self.store.list():
            if j.get("mode") == "live" and j.get("streamer_id") and j["status"] in ("queued", "running"):
                out[j["streamer_id"]] = j["id"]
        return out
