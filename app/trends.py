"""Hashtags e palavras-chave em alta no nicho (ex.: GTA 6).

Fontes:
- YouTube: Shorts mais vistos das últimas 72 h sobre as palavras-chave (API oficial; ~2 consultas/dia).
- Manuais: as que o usuário copiar do TikTok Creative Center (o TikTok não tem API para isso).
- Fixas: as do nicho, sempre presentes.
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

from .config import DATA_DIR
from .store import get_config, get_secret

TRENDS_PATH = DATA_DIR / "trends.json"
REFRESH_HOURS = 12
_HASHTAG = re.compile(r"#([\wÀ-ɏ]{2,40})", re.UNICODE)


def load() -> dict:
    try:
        return json.loads(TRENDS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"updated_at": 0, "youtube": [], "keywords": [], "error": None}


def _youtube_client():
    from googleapiclient.discovery import build
    key = get_secret("YOUTUBE_API_KEY")
    if key:
        return build("youtube", "v3", developerKey=key, cache_discovery=False)
    from .publish_youtube import load_credentials
    creds = load_credentials()
    if not creds:
        return None
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def refresh(force: bool = False) -> dict:
    data = load()
    if not force and time.time() - data.get("updated_at", 0) < REFRESH_HOURS * 3600:
        return data
    cfg = get_config()
    kws = cfg["monitor"]["keywords"][:2] or ["gta 6"]
    try:
        yt = _youtube_client()
        if yt is None:
            data["error"] = "Conecte o YouTube (ou cadastre uma YOUTUBE_API_KEY) para buscar tendências."
            return data
        since = (datetime.now(timezone.utc) - timedelta(hours=72)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ids = []
        for kw in kws:
            res = yt.search().list(q=kw, part="id", type="video", videoDuration="short", order="viewCount",
                                   publishedAfter=since, regionCode="BR", relevanceLanguage="pt",
                                   maxResults=50).execute()
            ids += [it["id"]["videoId"] for it in res.get("items", []) if it.get("id", {}).get("videoId")]
        tags, words = Counter(), Counter()
        for i in range(0, len(ids), 50):
            vids = yt.videos().list(id=",".join(ids[i:i + 50]), part="snippet,statistics").execute()
            for v in vids.get("items", []):
                views = int(v.get("statistics", {}).get("viewCount", 0) or 0)
                weight = 1 + views ** 0.5 / 100  # vídeos mais vistos pesam mais, sem dominar
                sn = v.get("snippet", {})
                for h in set(t.lower() for t in _HASHTAG.findall(f"{sn.get('title', '')} {sn.get('description', '')}")):
                    tags[h] += weight
                for t in sn.get("tags", []) or []:
                    words[t.lower().strip()] += weight
        data = {
            "updated_at": time.time(),
            "youtube": [{"tag": t, "score": round(s, 1)} for t, s in tags.most_common(25)],
            "keywords": [{"tag": t, "score": round(s, 1)} for t, s in words.most_common(25)],
            "sample": len(ids), "error": None,
        }
    except Exception as exc:
        data["error"] = f"Não foi possível buscar tendências: {str(exc)[:200]}"
    TRENDS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


_RELEVANT = re.compile(r"gta|grand ?theft|rockstar|vice ?city|leonida|lucia|jason", re.IGNORECASE)


def _relevant(tag: str) -> bool:
    return bool(_RELEVANT.search(tag or ""))


def hashtag_pool() -> dict:
    """Tudo que o gerador de legendas pode usar, separado por origem."""
    cfg = get_config()["publish"]
    data = load()
    uniq = lambda xs: list(dict.fromkeys(t.lstrip("#").lower() for t in xs if t.strip()))  # noqa: E731
    return {
        "fixas": uniq(cfg["fixed_hashtags"]),
        "manuais_tiktok": uniq(cfg["manual_hashtags"]),
        # só o que é do assunto: "em alta" geral (ishowspeed, football, trending...) num corte de GTA é metadado
        # enganoso para o YouTube e pode reduzir a distribuição
        "em_alta_youtube": [t["tag"] for t in data.get("youtube", []) if _relevant(t["tag"])][:12],
        "palavras_chave_youtube": [t["tag"] for t in data.get("keywords", []) if _relevant(t["tag"])][:12],
    }
