"""Envio para o TikTok pela Content Posting API oficial.

Modos:
- draft  : manda para a caixa de entrada/rascunhos do app (escopo video.upload). Você abre o TikTok e
           toca em publicar. Funciona antes da auditoria; no máximo 5 pendentes a cada 24 h.
- direct : publica direto (escopo video.publish). Antes da auditoria do app o TikTok deixa o post
           privado e só aceita contas privadas.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

import requests

from .store import CRED_DIR, get_secret

TOKEN = CRED_DIR / "tiktok_token.json"
API = "https://open.tiktokapis.com"
AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = f"{API}/v2/oauth/token/"
SCOPES = "user.info.basic,video.upload,video.publish"
CALLBACK_PATH = "/oauth/tiktok/callback/"

_pending: dict[str, dict] = {}
_state = {"error": None, "pkce": "hex"}  # a doc do TikTok pede hex; se falhar, tenta base64url


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    if _state["pkce"] == "hex":
        return digest.hex()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _load() -> dict | None:
    try:
        return json.loads(TOKEN.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save(tok: dict) -> None:
    tok["expires_at"] = time.time() + float(tok.get("expires_in", 7200))
    tok["refresh_expires_at"] = time.time() + float(tok.get("refresh_expires_in", 180 * 86400))
    TOKEN.write_text(json.dumps(tok), encoding="utf-8")


def status() -> dict:
    tok = _load()
    return {"has_keys": bool(get_secret("TIKTOK_CLIENT_KEY") and get_secret("TIKTOK_CLIENT_SECRET")),
            "connected": bool(tok), "scopes": (tok or {}).get("scope", ""),
            "error": _state["error"], "callback_path": CALLBACK_PATH}


def authorize_url(base_url: str) -> str:
    key = get_secret("TIKTOK_CLIENT_KEY")
    if not key:
        raise RuntimeError("Cadastre o Client key e o Client secret do app do TikTok.")
    verifier = secrets.token_urlsafe(64)[:96]
    state = secrets.token_urlsafe(16)
    redirect = base_url.rstrip("/") + CALLBACK_PATH
    _pending[state] = {"verifier": verifier, "redirect": redirect, "t": time.time()}
    _state["error"] = None
    q = {"client_key": key, "scope": SCOPES, "response_type": "code", "redirect_uri": redirect,
         "state": state, "code_challenge": _challenge(verifier), "code_challenge_method": "S256"}
    return AUTH_URL + "?" + urlencode(q)


def handle_callback(params: dict) -> str:
    if params.get("error"):
        _state["error"] = f"{params.get('error')}: {params.get('error_description', '')}"
        return _state["error"]
    pend = _pending.pop(params.get("state", ""), None)
    if not pend:
        _state["error"] = "Login expirado ou inválido. Clique em Conectar de novo."
        return _state["error"]
    r = requests.post(TOKEN_URL, timeout=30, headers={"Content-Type": "application/x-www-form-urlencoded"}, data={
        "client_key": get_secret("TIKTOK_CLIENT_KEY"), "client_secret": get_secret("TIKTOK_CLIENT_SECRET"),
        "code": params.get("code", ""), "grant_type": "authorization_code",
        "redirect_uri": pend["redirect"], "code_verifier": pend["verifier"]})
    data = r.json() if r.content else {}
    if r.status_code != 200 or "access_token" not in data:
        msg = data.get("error_description") or data.get("error") or r.text[:200]
        if "verifier" in str(msg).lower() or "challenge" in str(msg).lower():
            _state["pkce"] = "b64" if _state["pkce"] == "hex" else "hex"
            msg += " (ajustei o formato do código de segurança; clique em Conectar de novo)"
        _state["error"] = f"O TikTok recusou o login: {msg}"
        return _state["error"]
    _save(data)
    return "ok"


def disconnect() -> None:
    TOKEN.unlink(missing_ok=True)


def _access_token() -> str:
    tok = _load()
    if not tok:
        raise RuntimeError("TikTok não conectado.")
    if time.time() > tok.get("expires_at", 0) - 300:
        r = requests.post(TOKEN_URL, timeout=30, headers={"Content-Type": "application/x-www-form-urlencoded"},
                          data={"client_key": get_secret("TIKTOK_CLIENT_KEY"),
                                "client_secret": get_secret("TIKTOK_CLIENT_SECRET"),
                                "grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
        data = r.json() if r.content else {}
        if "access_token" not in data:
            raise RuntimeError("A conexão com o TikTok expirou. Conecte de novo na aba Contas.")
        _save(data)  # o TikTok troca o refresh_token a cada renovação
        tok = data
    return tok["access_token"]


def _call(path: str, body: dict | None = None) -> dict:
    r = requests.post(API + path, json=body or {}, timeout=60,
                      headers={"Authorization": f"Bearer {_access_token()}",
                               "Content-Type": "application/json; charset=UTF-8"})
    data = r.json() if r.content else {}
    err = data.get("error") or {}
    if r.status_code != 200 or err.get("code") not in (None, "ok"):
        code = err.get("code") or r.status_code
        raise RuntimeError(_friendly(code, err.get("message") or r.text[:200]))
    return data.get("data") or {}


def _friendly(code, msg: str) -> str:
    known = {
        "unaudited_client_can_only_post_to_private_accounts":
            "Antes da auditoria do app, a postagem direta só funciona em conta privada. Use o modo rascunho.",
        "spam_risk_too_many_posts": "Limite diário de postagens pela API atingido para esta conta.",
        "spam_risk_too_many_pending_share": "Já existem 5 rascunhos pendentes no app. Publique ou apague alguns.",
        "spam_risk_user_banned_from_posting": "O TikTok bloqueou temporariamente postagens desta conta.",
        "reached_active_user_cap": "O app atingiu o limite diário de usuários publicando.",
        "privacy_level_option_mismatch": "A privacidade escolhida não está disponível para esta conta.",
        "scope_not_authorized": "Permissão não concedida. Conecte o TikTok de novo e aceite todas as permissões.",
        "access_token_invalid": "A conexão com o TikTok expirou. Conecte de novo.",
        "rate_limit_exceeded": "Muitas tentativas seguidas. O robô tenta de novo mais tarde.",
    }
    return known.get(str(code), f"TikTok: {code} — {msg}")


def post(path: Path, caption: str, mode: str = "draft") -> dict:
    size = path.stat().st_size
    source = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": size, "total_chunk_count": 1}
    if mode == "direct":
        info = _call("/v2/post/publish/creator_info/query/")
        options = info.get("privacy_level_options") or []
        privacy = "PUBLIC_TO_EVERYONE" if "PUBLIC_TO_EVERYONE" in options else (options[0] if options else "SELF_ONLY")
        data = _call("/v2/post/publish/video/init/", {
            "post_info": {"title": caption[:2200], "privacy_level": privacy,
                          "disable_duet": False, "disable_comment": False, "disable_stitch": False,
                          "video_cover_timestamp_ms": 1000},
            "source_info": source})
    else:
        data = _call("/v2/post/publish/inbox/video/init/", {"source_info": source})

    with path.open("rb") as fh:
        r = requests.put(data["upload_url"], data=fh, timeout=600, headers={
            "Content-Type": "video/mp4", "Content-Length": str(size),
            "Content-Range": f"bytes 0-{size - 1}/{size}"})
    if r.status_code >= 300:
        raise RuntimeError(f"Falha ao enviar o arquivo para o TikTok (HTTP {r.status_code}).")

    status = "enviado"
    for _ in range(12):  # acompanha o processamento por até ~1 minuto
        time.sleep(5)
        try:
            st = _call("/v2/post/publish/status/fetch/", {"publish_id": data["publish_id"]})
        except Exception:
            break
        status = st.get("status") or status
        if "FAIL" in str(status).upper():
            raise RuntimeError(f"O TikTok não aceitou o vídeo: {st.get('fail_reason') or status}")
        if any(k in str(status).upper() for k in ("COMPLETE", "INBOX", "PUBLISHED")):
            break
    return {"publish_id": data["publish_id"], "status": status, "mode": mode}
