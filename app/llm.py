"""Escolha automática da IA: Claude Haiku (quando há chave e saldo) ou IA local grátis (Ollama).

- Claude Haiku 5.5: melhor qualidade, custa centavos por live.
- Ollama (local, no seu PC): grátis, sem limite, um pouco abaixo em qualidade.
Se o Claude responder "sem créditos" ou "chave inválida", ele fica de lado por 1 hora e a IA local assume.
"""
from __future__ import annotations

import json
import os
import time
from typing import Callable, TypeVar

import anthropic
import requests
from pydantic import BaseModel

from .config import CLAUDE_MODEL

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:3b")
T = TypeVar("T", bound=BaseModel)

_claude_paused_until = 0.0
_claude_pause_reason = ""


class NoAI(Exception):
    """Nenhuma IA disponível; o chamador usa o modo simples (volume e chat)."""


# ---------------------------------------------------------------- disponibilidade

def claude_configured() -> bool:
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    try:
        c = anthropic.Anthropic()  # também lê o login feito com `ant auth login`
        return bool(c.api_key or c.auth_token or c.credentials)
    except Exception:
        return False


def claude_ready() -> bool:
    return claude_configured() and time.time() >= _claude_paused_until


def ollama_ready() -> bool:
    try:
        tags = requests.get(f"{OLLAMA_URL}/api/tags", timeout=2).json().get("models", [])
        names = {m.get("name", "") for m in tags}
        return OLLAMA_MODEL in names or f"{OLLAMA_MODEL}:latest" in names
    except Exception:
        return False


def status() -> dict:
    claude = claude_configured()
    paused = claude and time.time() < _claude_paused_until
    local = ollama_ready()
    active = "claude" if (claude and not paused) else "local" if local else "none"
    return {"active": active, "claude_configured": claude, "claude_paused": paused,
            "claude_pause_reason": _claude_pause_reason if paused else "", "claude_model": CLAUDE_MODEL,
            "local_ready": local, "local_model": OLLAMA_MODEL,
            "groq_configured": bool(groq_key()), "groq_paused": time.time() < _groq_paused_until,
            "groq_pause_reason": _groq_pause_reason if time.time() < _groq_paused_until else "",
            "groq_model": _groq_model_ok}


def friendly_error(exc: Exception) -> str:
    msg = str(exc)
    if isinstance(exc, anthropic.AuthenticationError) or "authentication method" in msg:
        return "chave do Claude ausente ou inválida"
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "a chave do Claude não tem permissão para este modelo"
    if isinstance(exc, anthropic.RateLimitError):
        return "limite de uso do Claude atingido"
    if isinstance(exc, anthropic.BadRequestError) and "credit" in msg.lower():
        return "sem créditos na conta do Claude"
    if isinstance(exc, anthropic.APIConnectionError):
        return "sem conexão com o Claude"
    if isinstance(exc, requests.RequestException):
        return "a IA local (Ollama) não respondeu"
    return msg[:200]


# ---------------------------------------------------------------- chamadas

def claude_parse(system: str, prompt: str, schema: type[T], effort: str = "low") -> T:
    """Claude Haiku com saída estruturada (já validada pelo SDK)."""
    client = anthropic.Anthropic(max_retries=3)
    resp = client.messages.parse(
        model=CLAUDE_MODEL,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": prompt}],
        output_format=schema,
        output_config={"effort": effort},
    )
    if resp.stop_reason == "refusal" or resp.parsed_output is None:
        raise RuntimeError("o Claude não respondeu a este pedido")
    return resp.parsed_output


def ollama_parse(system: str, prompt: str, schema: type[T], num_ctx: int = 8192) -> T:
    """IA local com a resposta presa ao formato do schema (JSON Schema no campo `format`)."""
    r = requests.post(f"{OLLAMA_URL}/api/chat", timeout=300, json={
        "model": OLLAMA_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        "stream": False,
        "format": schema.model_json_schema(),
        "options": {"temperature": 0.3, "num_ctx": num_ctx},
        "keep_alive": "2m",  # libera a placa de vídeo logo depois, para a transcrição
    })
    r.raise_for_status()
    return schema.model_validate_json(r.json()["message"]["content"])


# ---------------------------------------------------------------- Groq (grátis, na nuvem): títulos finais
# O plano gratuito tem limite diário, então a Groq só escreve o título dos cortes que vão para a Agenda
# (a escolha dos momentos, que pede muitas chamadas, continua na IA local).
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODELS = ["openai/gpt-oss-120b", "llama-3.3-70b-versatile", "openai/gpt-oss-20b"]
_groq_paused_until = 0.0
_groq_pause_reason = ""
_groq_model_ok: str | None = None


def groq_key() -> str:
    from .store import get_secret
    return get_secret("GROQ_API_KEY")


def groq_ready() -> bool:
    # pausa curta (limite por minuto) conta como pronta: as chamadas esperam os segundos e seguem
    return bool(groq_key()) and _groq_paused_until - time.time() <= SHORT_WAIT


def _json_block(text: str) -> str:
    """Tira o JSON de uma resposta que veio com texto ou ```json em volta."""
    a, b = text.find("{"), text.rfind("}")
    return text[a:b + 1] if a >= 0 and b > a else text


SHORT_WAIT = 65  # limite por minuto do plano grátis: vale esperar até ~1 min em vez de desistir


def _retry_after(r) -> float:
    try:
        return float(r.headers.get("retry-after") or 0)
    except ValueError:
        return 0.0


def _groq_post(body: dict, paused_attr: str):
    """POST na Groq que espera o limite POR MINUTO (segundos) e tenta de novo, até 3 vezes.
    Devolve a resposta; se o limite for longo (diário), devolve o 429 para o chamador pausar."""
    for attempt in range(3):
        wait = globals()[paused_attr] - time.time()
        if 0 < wait <= SHORT_WAIT:
            time.sleep(wait + 0.5)
        r = requests.post(GROQ_URL, json=body, timeout=90,
                          headers={"Authorization": f"Bearer {groq_key()}", "Content-Type": "application/json"})
        if r.status_code != 429:
            return r
        wait = _retry_after(r) or 20.0
        if wait > SHORT_WAIT or attempt == 2:
            return r
        time.sleep(wait + 0.5)
    return r


def groq_parse(system: str, prompt: str, schema: type[T]) -> T:
    """Groq com resposta no formato do schema (validada aqui: alguns modelos ignoram o formato pedido)."""
    global _groq_paused_until, _groq_pause_reason, _groq_model_ok
    from .store import get_secret
    custom = get_secret("GROQ_MODEL")
    models = [custom] if custom else ([_groq_model_ok] if _groq_model_ok else []) + \
        [m for m in GROQ_MODELS if m != _groq_model_ok]
    sys_full = system + "\n\nResponda SOMENTE com um objeto JSON neste formato (JSON Schema):\n" + \
        json.dumps(schema.model_json_schema(), ensure_ascii=False)
    last = "nenhum modelo respondeu"
    for model in models:
        body = {"model": model, "temperature": 0.4, "max_completion_tokens": 2500,
                "messages": [{"role": "system", "content": sys_full}, {"role": "user", "content": prompt}],
                "response_format": {"type": "json_schema",
                                    "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema()}}}
        if "gpt-oss" in model:
            body["reasoning_effort"] = "low"
        r = _groq_post(body, "_groq_paused_until")
        if r.status_code == 429:  # limite longo (diário): pausa o tempo que a Groq pedir (máx. 6 h)
            wait = _retry_after(r) or 900
            _groq_paused_until = time.time() + min(max(wait, 3), 6 * 3600)
            _groq_pause_reason = "limite gratuito da Groq atingido; volta sozinha"
            raise RuntimeError(_groq_pause_reason)
        if r.status_code in (401, 403):
            _groq_paused_until, _groq_pause_reason = time.time() + 3600, "chave da Groq inválida"
            raise RuntimeError(_groq_pause_reason)
        if r.status_code >= 400:  # modelo indisponível ou sem suporte ao formato: tenta o próximo
            last = f"{model}: HTTP {r.status_code} {r.text[:160]}"
            continue
        try:
            out = schema.model_validate_json(_json_block(r.json()["choices"][0]["message"]["content"] or ""))
            _groq_model_ok = model
            return out
        except Exception as exc:
            last = f"{model}: resposta fora do formato ({str(exc)[:120]})"
    raise RuntimeError(f"Groq falhou: {last}")


GROQ_VISION_MODELS = ["qwen/qwen3.8-27b"]
_vision_paused_until = 0.0


def groq_vision(prompt: str, jpeg: bytes) -> str:
    """Descrição de imagem pela Groq (modelo que entende imagem). Vazio se não der: o título sai sem ela."""
    global _vision_paused_until
    import base64
    from .store import get_secret
    if not groq_key() or _vision_paused_until - time.time() > SHORT_WAIT:
        return ""
    models = [m for m in [get_secret("GROQ_VISION_MODEL")] if m] or GROQ_VISION_MODELS
    url = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
    for model in models:
        try:
            r = _groq_post({"model": model, "temperature": 0.2, "max_completion_tokens": 700,
                            "messages": [{"role": "user", "content": [
                                {"type": "text", "text": prompt},
                                {"type": "image_url", "image_url": {"url": url}}]}]}, "_vision_paused_until")
        except requests.RequestException as exc:
            print(f"[ia] visão: {exc}")
            continue
        if r.status_code == 429:  # limite longo: pausa a visão (o título sai sem a descrição da tela)
            wait = _retry_after(r) or 600
            _vision_paused_until = time.time() + min(max(wait, 3), 6 * 3600)
            return ""
        if r.status_code >= 400:
            print(f"[ia] visão: {model} HTTP {r.status_code} {r.text[:120]}")
            continue
        text = (r.json()["choices"][0]["message"].get("content") or "").strip()
        if "</think>" in text:  # alguns modelos devolvem o raciocínio antes da resposta
            text = text.split("</think>")[-1].strip()
        return text[:500]
    return ""


def title_parse(system: str, prompt: str, schema: type[T], cloud: bool = True) -> T:
    """Para títulos: Groq (melhor e grátis) quando disponível; senão a IA local."""
    for _ in range(3):
        # limite POR MINUTO do plano grátis: libera em segundos, vale esperar em vez de cair na IA local
        wait = _groq_paused_until - time.time()
        if not (cloud and groq_key() and 0 < wait <= 90):
            break
        time.sleep(wait + 1)
        if cloud and groq_ready():
            try:
                return groq_parse(system, prompt, schema)
            except Exception as exc:
                print(f"[ia] Groq: {exc}")
    if cloud and groq_ready():
        try:
            return groq_parse(system, prompt, schema)
        except Exception as exc:
            print(f"[ia] Groq: {exc}; usando a IA local")
    return ollama_parse(system, prompt, schema, num_ctx=4096)


def run(claude_fn: Callable[[], T], local_fn: Callable[[], T]) -> tuple[T, str]:
    """Tenta o Claude; se não der, a IA local. Retorna (resultado, nome da IA usada)."""
    global _claude_paused_until, _claude_pause_reason
    errors = []
    if claude_ready():
        try:
            return claude_fn(), "Claude Haiku"
        except Exception as exc:
            reason = friendly_error(exc)
            errors.append(reason)
            if any(k in reason for k in ("créditos", "chave", "permissão")):
                _claude_paused_until, _claude_pause_reason = time.time() + 3600, reason
            print(f"[ia] Claude falhou ({reason}); tentando a IA local")
    if ollama_ready():
        try:
            return local_fn(), "IA local"
        except Exception as exc:
            errors.append(friendly_error(exc))
            print(f"[ia] IA local falhou: {exc}")
    else:
        errors.append("IA local (Ollama) não está rodando")
    raise NoAI("; ".join(errors))
