"""Avisos no Telegram: legenda pronta para colar quando um rascunho chega no TikTok, e falhas de envio."""
from __future__ import annotations

import html

import requests

from .store import get_secret, set_secrets

API = "https://api.telegram.org/bot{token}/{method}"


def _call(method: str, **params) -> dict:
    token = get_secret("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Cadastre o token do bot do Telegram na aba Contas.")
    r = requests.post(API.format(token=token, method=method), json=params, timeout=20)
    data = r.json() if r.content else {}
    if not data.get("ok"):
        desc = data.get("description", f"HTTP {r.status_code}")
        if r.status_code == 401:
            desc = "token do bot inválido"
        raise RuntimeError(f"Telegram: {desc}")
    return data.get("result")


def configured() -> bool:
    return bool(get_secret("TELEGRAM_BOT_TOKEN") and get_secret("TELEGRAM_CHAT_ID"))


def status() -> dict:
    return {"has_token": bool(get_secret("TELEGRAM_BOT_TOKEN")), "has_chat": bool(get_secret("TELEGRAM_CHAT_ID"))}


def detect_chat() -> str:
    """Pega o chat de quem mandou a última mensagem para o bot (você manda /start antes)."""
    updates = _call("getUpdates", limit=50, timeout=0) or []
    chats = [u.get("message", {}).get("chat") for u in updates if u.get("message")]
    chats = [c for c in chats if c and c.get("type") == "private"]
    if not chats:
        raise RuntimeError("Nenhuma mensagem encontrada. Abra o seu bot no Telegram, envie /start e tente de novo.")
    chat = chats[-1]
    set_secrets({"TELEGRAM_CHAT_ID": str(chat["id"])})
    return chat.get("first_name") or chat.get("username") or str(chat["id"])


def send(text_html: str) -> None:
    _call("sendMessage", chat_id=get_secret("TELEGRAM_CHAT_ID"), text=text_html,
          parse_mode="HTML", disable_web_page_preview=True)


def draft_ready(post: dict) -> None:
    """Rascunho chegou no TikTok: manda a legenda num bloco que copia com um toque."""
    who = f" — {html.escape(post['streamer'])}" if post.get("streamer") else ""
    send(f"🎬 <b>Rascunho novo no TikTok</b>{who}\n"
         f"Abra a notificação do TikTok e cole esta legenda (toque no texto para copiar):\n\n"
         f"<code>{html.escape(post['caption'])}</code>")


def send_failed(post: dict, final: bool) -> None:
    plat = "YouTube" if post["platform"] == "youtube" else "TikTok"
    what = "falhou de vez" if final else "falhou; nova tentativa em 15 minutos"
    title = post["caption"] if post["platform"] == "tiktok" else post["title"]
    send(f"⚠️ <b>Envio para o {plat} {what}</b>\n{html.escape(title[:120])}\n\n"
         f"Motivo: {html.escape(post.get('error') or '')}")


def test() -> None:
    send("✅ <b>Cortador conectado!</b>\nAs legendas dos rascunhos do TikTok vão chegar aqui. "
         "Toque no texto abaixo para testar a cópia:\n\n<code>Exemplo de legenda #gta6 #cortes</code>")
