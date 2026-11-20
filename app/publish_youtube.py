"""Envio de Shorts pela API oficial do YouTube (OAuth de app para computador)."""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path

from .store import CRED_DIR

CLIENT_SECRET = CRED_DIR / "youtube_client_secret.json"
TOKEN = CRED_DIR / "youtube_token.json"
ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly",
          ANALYTICS_SCOPE]  # retenção (quanto do Short as pessoas assistem)

_state = {"connecting": False, "error": None, "channel": None}


def load_credentials():
    if not TOKEN.exists():
        return None
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    # sem forçar escopos: vale a permissão que a conta deu (contas conectadas antes não têm a de retenção)
    creds = Credentials.from_authorized_user_file(str(TOKEN))
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN.write_text(creds.to_json(), encoding="utf-8")
    return creds if creds.valid else None


def has_analytics() -> bool:
    try:
        import json
        return ANALYTICS_SCOPE in (json.loads(TOKEN.read_text(encoding="utf-8")).get("scopes") or [])
    except Exception:
        return False


def analytics_client():
    from googleapiclient.discovery import build
    creds = load_credentials()
    if not creds or not has_analytics():
        return None
    return build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)


def _client():
    from googleapiclient.discovery import build
    creds = load_credentials()
    if not creds:
        raise RuntimeError("YouTube não conectado.")
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def status() -> dict:
    connected = TOKEN.exists()
    if connected and not _state["channel"]:
        try:
            res = _client().channels().list(mine=True, part="snippet").execute()
            items = res.get("items", [])
            _state["channel"] = items[0]["snippet"]["title"] if items else "(sem canal)"
        except Exception as exc:
            _state["error"] = str(exc)[:200]
    return {"has_client_secret": CLIENT_SECRET.exists(), "connected": connected,
            "analytics": connected and has_analytics(),
            "connecting": _state["connecting"], "channel": _state["channel"], "error": _state["error"]}


def save_client_secret(content: bytes) -> None:
    import json
    data = json.loads(content.decode("utf-8"))
    if "installed" not in data:
        raise ValueError("Use um ID do cliente OAuth do tipo “App para computador” (Desktop app).")
    CLIENT_SECRET.write_bytes(content)


def connect() -> None:
    """Abre o navegador para o login do Google (roda em segundo plano)."""
    if not CLIENT_SECRET.exists():
        raise RuntimeError("Envie primeiro o arquivo client_secret.json do Google Cloud.")
    if _state["connecting"]:
        return

    def run():
        _state.update(connecting=True, error=None, channel=None)
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow
            flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
            creds = flow.run_local_server(port=0, open_browser=True, prompt="consent",
                                          success_message="YouTube conectado. Pode fechar esta aba.",
                                          timeout_seconds=300)
            TOKEN.write_text(creds.to_json(), encoding="utf-8")
        except Exception as exc:
            _state["error"] = f"Não foi possível conectar: {str(exc)[:200]}"
        finally:
            _state["connecting"] = False

    threading.Thread(target=run, daemon=True).start()


def disconnect() -> None:
    TOKEN.unlink(missing_ok=True)
    _state.update(channel=None, error=None)


def upload(path: Path, title: str, description: str, tags: list[str],
           privacy: str = "public", publish_at: datetime | None = None) -> dict:
    """Envia o vídeo. Com publish_at, o YouTube publica sozinho na hora marcada."""
    from googleapiclient.http import MediaFileUpload
    body = {
        "snippet": {"title": title[:100], "description": description[:4900],
                    "tags": _fit_tags(tags), "categoryId": "20", "defaultLanguage": "pt-BR"},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    if publish_at and publish_at > datetime.now(timezone.utc):
        body["status"]["privacyStatus"] = "private"  # exigido pelo YouTube para agendar
        body["status"]["publishAt"] = publish_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    media = MediaFileUpload(str(path), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    req = _client().videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    vid = resp["id"]
    return {"id": vid, "url": f"https://youtube.com/shorts/{vid}",
            "privacy": resp.get("status", {}).get("privacyStatus")}


def _fit_tags(tags: list[str]) -> list[str]:
    out, total = [], 0
    for t in tags:
        t = t.strip().lstrip("#")
        if not t or t in out:
            continue
        cost = len(t) + (2 if " " in t else 0) + 1
        if total + cost > 480:
            break
        out.append(t)
        total += cost
    return out
