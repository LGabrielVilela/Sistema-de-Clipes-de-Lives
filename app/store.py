"""Configurações do robô (data/config.json) e chaves de API (data/secrets.json)."""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path

from .config import DATA_DIR, json_default

CONFIG_PATH = DATA_DIR / "config.json"
SECRETS_PATH = DATA_DIR / "secrets.json"
CRED_DIR = DATA_DIR / "credentials"
CRED_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_CONFIG = {
    "monitor": {
        "enabled": False,
        "interval_min": 3,
        "keywords": ["gta 6", "gta vi", "gta6", "grand theft auto vi"],
        "max_concurrent": 2,
        "block_minutes": 15,
        "max_hours": 8,
        "clips_per_block": 3,
        "min_len": 27,  # cortes de ~30 s
        "max_len": 33,
        "layout": "auto",
        "cold_open": True,  # abre o corte com 2-3 s do clímax
        "loop_end": True,   # final emendado no começo
        "turn_minutes": 60,  # rodízio: com gente esperando, cada gravação cede a vaga depois disso (0 = sem rodízio)
        "min_score": 55,    # nota mínima de um momento para virar corte (além de estar acima do normal)
    },
    "streamers": [],  # {id, platform, handle, name, any_game, enabled}
    "publish": {
        "youtube": {"enabled": False, "privacy": "public"},
        "tiktok": {"enabled": False, "mode": "draft"},  # draft = rascunho no app; direct = posta direto
        "slots": ["12:00", "18:00", "21:00"],
        "min_score": 60,
        "auto_approve": False,  # cortes com nota >= auto_min_score vão direto para a agenda
        "auto_min_score": 75,
        "fixed_hashtags": ["gta6", "gtavi", "cortes", "gta"],
        "manual_hashtags": [],
        "credit_line": True,
        "auto_slots": True,  # troca horários que rendem pouco pelos que rendem mais (aprender com as views)
    },
}

SECRET_KEYS = ["ANTHROPIC_API_KEY", "TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET",
               "KICK_CLIENT_ID", "KICK_CLIENT_SECRET", "TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET",
               "YOUTUBE_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "GROQ_API_KEY"]

_lock = threading.RLock()


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return copy.deepcopy(default)


def _write(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=json_default), encoding="utf-8")
    tmp.replace(path)


def _merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def get_config() -> dict:
    with _lock:
        return _merge(DEFAULT_CONFIG, _read(CONFIG_PATH, {}))


def update_config(patch: dict) -> dict:
    with _lock:
        cfg = _merge(get_config(), patch)
        _write(CONFIG_PATH, cfg)
        return cfg


# ---------------------------------------------------------------- chaves

def get_secret(name: str) -> str:
    with _lock:
        val = _read(SECRETS_PATH, {}).get(name)
    return (val or os.environ.get(name) or "").strip()


def set_secrets(values: dict) -> None:
    with _lock:
        data = _read(SECRETS_PATH, {})
        for k, v in values.items():
            if k not in SECRET_KEYS or v is None:
                continue
            v = v.strip()
            if v:
                data[k] = v
                os.environ[k] = v  # vale na hora, sem reiniciar
            else:
                data.pop(k, None)
                os.environ.pop(k, None)
        _write(SECRETS_PATH, data)


def secrets_status() -> dict:
    """Só diz quais chaves existem (nunca devolve o valor para o navegador)."""
    return {k: bool(get_secret(k)) for k in SECRET_KEYS}


def load_secrets_into_env() -> None:
    for k in SECRET_KEYS:
        v = _read(SECRETS_PATH, {}).get(k)
        if v and not os.environ.get(k):
            os.environ[k] = v


load_secrets_into_env()
