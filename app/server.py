"""Servidor web local (FastAPI) do painel de cortes."""
from __future__ import annotations

import os
import socket
import threading
import webbrowser
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import HOST, JOBS_DIR, PORT, WEB_DIR
from .media import ffmpeg_ok
from . import monitor as mon
from . import llm, notify_telegram, publish_tiktok, publish_youtube, trends
from .pipeline import get_store
from .scheduler import get_scheduler
from .store import get_config, secrets_status, set_secrets, update_config
from .transcriber import whisper_device

app = FastAPI(title="Sistema de Cortes")
_jobs = get_store()  # inicia a fila de processamento
_sched = get_scheduler()  # fila de postagem
_jobs.on_clips_ready = _sched.on_clips_ready
_monitor = mon.Monitor(_jobs)  # vigia de streamers


def _protected_jobs() -> set[str]:
    """Jobs cujos cortes ainda são usados: post pendente, agendado, enviando ou enviado há menos de 7 dias."""
    import time
    now = time.time()
    return {p["job_id"] for p in _sched.list()
            if p["status"] in ("pending", "scheduled", "sending")
            or (p["status"] == "done" and now - (p.get("sent_at") or 0) < 7 * 86400)}


def _cleanup_loop() -> None:
    """Limpeza automática do disco a cada 30 min (a primeira 2 min depois de abrir)."""
    import time
    import traceback
    time.sleep(120)
    while True:
        try:
            _jobs.auto_cleanup(_protected_jobs())
        except Exception:
            traceback.print_exc()
        time.sleep(1800)


threading.Thread(target=_cleanup_loop, daemon=True).start()


class Settings(BaseModel):
    num_clips: int = Field(5, ge=1, le=20)
    min_len: int = Field(25, ge=5, le=170)
    max_len: int = Field(60, ge=10, le=180)
    layout: Literal["auto", "split", "crop", "blur"] = "auto"
    language: str = "pt"
    live_minutes: int = Field(120, ge=1, le=720)
    block_minutes: int = Field(15, ge=3, le=120)
    title_overlay: bool = True
    cold_open: bool = True
    loop_end: bool = True
    section_start: float | None = Field(None, ge=0)
    section_end: float | None = Field(None, gt=0)


class NewJob(BaseModel):
    url: str
    mode: Literal["video", "live"] = "video"
    settings: Settings = Settings()


class ClipEdit(BaseModel):
    start: float
    end: float
    title: str | None = None


def _err(exc: Exception, code: int = 400):
    raise HTTPException(code, str(exc))


def _ai_configured() -> bool:
    """Chave colada no painel/.env OU login feito com `ant auth login` (a SDK lê o perfil sozinha)."""
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    try:
        import anthropic
        c = anthropic.Anthropic()
        return bool(c.api_key or c.auth_token or c.credentials)
    except Exception:
        return False


@app.get("/api/health")
def health():
    try:
        import cv2  # noqa: F401
        cv_ok = True
    except Exception:
        cv_ok = False
    return {
        "ffmpeg": ffmpeg_ok(),
        "ai_key": llm.status()["active"] != "none",
        "ai": llm.status(),
        "whisper": whisper_device(),
        "face": cv_ok,
    }


@app.get("/api/jobs")
def list_jobs():
    return get_store().list()


@app.post("/api/jobs")
def create_job(body: NewJob):
    if not body.url.lower().startswith(("http://", "https://")):
        _err(ValueError("Cole um link completo, começando com https://"))
    st = body.settings
    if st.min_len >= st.max_len:
        _err(ValueError("A duração mínima precisa ser menor que a máxima."))
    if st.section_start is not None and st.section_end is not None and st.section_end <= st.section_start:
        _err(ValueError("O fim do trecho precisa ser depois do início."))
    if body.mode == "live" and st.block_minutes > st.live_minutes:
        st.block_minutes = st.live_minutes
    return get_store().create(body.url, body.mode, st.model_dump())


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = get_store().get(job_id)
    if not job:
        _err(KeyError("Job não encontrado"), 404)
    return job


def _action(fn, *args):
    try:
        return fn(*args)
    except KeyError:
        _err(KeyError("Não encontrado"), 404)
    except Exception as exc:
        _err(exc)


@app.post("/api/jobs/cleanup")
def cleanup_jobs(dry_run: bool = True):
    return get_store().cleanup(_protected_jobs(), dry_run)


@app.post("/api/jobs/{job_id}/stop")
def stop_job(job_id: str):
    get_store().stop_live(job_id)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    _action(get_store().cancel, job_id)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str):
    return _action(get_store().retry, job_id)


@app.post("/api/jobs/{job_id}/reanalyze")
def reanalyze(job_id: str, settings: Settings):
    return _action(get_store().reanalyze, job_id, settings.model_dump(exclude={"section_start", "section_end"}))


@app.post("/api/jobs/{job_id}/clips/{index}")
def edit_clip(job_id: str, index: int, body: ClipEdit):
    return _action(get_store().rerender_clip, job_id, index, body.start, body.end, body.title)


@app.get("/api/jobs/{job_id}/zip")
def zip_job(job_id: str):
    data, name = _action(get_store().zip_clips, job_id)
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    _action(get_store().delete, job_id)
    return {"ok": True}


# ================================================================ robô: streamers
class StreamerIn(BaseModel):
    text: str
    platform: Literal["twitch", "kick", "youtube"] | None = None
    any_game: bool = False


class StreamerPatch(BaseModel):
    any_game: bool | None = None
    enabled: bool | None = None
    name: str | None = None
    cam_fixed: list[float] | None = None  # webcam fixa [esq, topo, dir, base] em pixels do vídeo original


@app.get("/api/monitor")
def monitor_state():
    cfg = get_config()
    return {"config": cfg["monitor"], "streamers": cfg["streamers"], "status": _monitor.status,
            "last_run": _monitor.last_run, "running": _monitor.running,
            "apis": {"twitch": mon.twitch_token.available(), "kick": mon.kick_token.available()}}


@app.post("/api/monitor/check")
def monitor_check():
    _monitor.check_now()
    return {"ok": True}


@app.post("/api/streamers")
def add_streamer(body: StreamerIn):
    s = _action(mon.add_streamer, body.text, body.platform, body.any_game)
    _monitor.check_now()
    return s


@app.patch("/api/streamers/{sid}")
def patch_streamer(sid: str, body: StreamerPatch):
    mon.edit_streamer(sid, **body.model_dump(exclude_none=True))
    return {"ok": True}


@app.delete("/api/streamers/{sid}")
def delete_streamer(sid: str):
    mon.remove_streamer(sid)
    return {"ok": True}


@app.patch("/api/config")
def patch_config(body: dict):
    allowed = {k: v for k, v in body.items() if k in ("monitor", "publish")}
    cfg = update_config(allowed)
    if "monitor" in allowed:
        _monitor.check_now()
    if "publish" in allowed:  # plataforma ativada: os cortes que já esperam ganham o post dela
        _sched.backfill([k for k in ("youtube", "tiktok") if cfg["publish"][k]["enabled"]])
    return cfg


@app.get("/api/config")
def read_config():
    return get_config()


# ================================================================ aprender com as views
@app.get("/api/insights")
def read_insights():
    from . import learn
    return learn.load() or {"measured": 0, "need": learn.MIN_POSTS, "ready": False, "findings": [], "top": []}


@app.post("/api/insights/refresh")
def refresh_insights():
    try:
        return _sched.refresh_learning(force=True)
    except Exception as exc:
        _err(exc)


# ================================================================ robô: agenda de posts
class PostEdit(BaseModel):
    title: str | None = None
    description: str | None = None
    caption: str | None = None
    tags: list[str] | None = None


class QueueIn(BaseModel):
    platforms: list[Literal["youtube", "tiktok"]]


@app.get("/api/posts")
def list_posts():
    return _sched.list()


@app.get("/api/posts/auto")
def auto_preview():
    return _sched.auto_preview()


@app.post("/api/posts/regenerate")
def regenerate_posts():
    return {"clips": _action(_sched.regenerate)}


@app.get("/api/posts/regenerate")
def regenerate_status():
    return {"running": getattr(_sched, "_regen_running", False), **getattr(_sched, "regen_progress", {})}


@app.post("/api/posts/approve-all")
def approve_all():
    return {"approved": _sched.approve_all()}


@app.post("/api/posts/{pid}/{action}")
def post_action(pid: str, action: Literal["approve", "now", "unschedule", "discard"], body: PostEdit | None = None):
    fn = {"approve": lambda: _sched.approve(pid, body.model_dump(exclude_none=True) if body else None),
          "now": lambda: _sched.post_now(pid), "unschedule": lambda: _sched.unschedule(pid),
          "discard": lambda: _sched.discard(pid)}[action]
    return _action(fn) or {"ok": True}


@app.patch("/api/posts/{pid}")
def edit_post(pid: str, body: PostEdit):
    return _action(_sched.edit, pid, body.model_dump(exclude_none=True))


@app.post("/api/jobs/{job_id}/clips/{index}/queue")
def queue_clip(job_id: str, index: int, body: QueueIn):
    import json as _json
    job = get_store().get(job_id)
    if not job or index >= len(job.get("clips", [])):
        _err(KeyError("Corte não encontrado"), 404)
    tr = _json.loads((JOBS_DIR / job_id / "transcript.json").read_text(encoding="utf-8"))
    _action(_sched.add_clip, job, index, tr, body.platforms)
    return {"ok": True}


@app.get("/api/trends")
def get_trends():
    return {**trends.load(), "pool": trends.hashtag_pool()}


@app.post("/api/trends/refresh")
def refresh_trends():
    return _action(trends.refresh, True)


# ================================================================ robô: contas
class SecretsIn(BaseModel):
    values: dict[str, str]


class FileIn(BaseModel):
    content: str


@app.get("/api/accounts")
def accounts():
    return {"secrets": secrets_status(), "ai_login": llm.claude_configured() and not secrets_status()["ANTHROPIC_API_KEY"],
            "ai": llm.status(),
            "youtube": publish_youtube.status(), "tiktok": publish_tiktok.status(),
            "telegram": notify_telegram.status()}


@app.post("/api/accounts/telegram/detect")
def telegram_detect():
    return {"name": _action(notify_telegram.detect_chat)}


@app.post("/api/accounts/telegram/test")
def telegram_test():
    _action(notify_telegram.test)
    return {"ok": True}


@app.post("/api/accounts/secrets")
def save_secrets(body: SecretsIn):
    set_secrets(body.values)
    mon.twitch_token.token = mon.kick_token.token = None  # força novo token com as chaves novas
    if "GROQ_API_KEY" in body.values:
        llm._groq_paused_until = 0.0  # chave nova: tenta de novo na hora
    return secrets_status()


@app.post("/api/accounts/groq/test")
def groq_test():
    """Testa a chave da Groq com um título de exemplo."""
    from .moments import TITLE_RULES, LocalTitle
    llm._groq_paused_until = 0.0
    try:
        t = llm.groq_parse(TITLE_RULES, 'TRECHO DO CORTE: "Mano, olha isso! O mapa do GTA 5 tá dentro do GTA 6, '
                                        'cara. Olha a praia, é igualzinha, não acredito que eles fizeram isso."',
                           LocalTitle)
    except Exception as exc:
        _err(exc)
    return {"ok": True, "title": t.titulo, "model": llm.status()["groq_model"]}


@app.post("/api/accounts/youtube/client-secret")
def youtube_secret(body: FileIn):
    _action(publish_youtube.save_client_secret, body.content.encode("utf-8"))
    return publish_youtube.status()


@app.post("/api/accounts/youtube/connect")
def youtube_connect():
    _action(publish_youtube.connect)
    return {"ok": True}


@app.post("/api/accounts/youtube/disconnect")
def youtube_disconnect():
    publish_youtube.disconnect()
    return {"ok": True}


@app.get("/api/accounts/tiktok/connect")
def tiktok_connect(request: Request):
    base = f"http://127.0.0.1:{request.url.port or PORT}"
    return {"url": _action(publish_tiktok.authorize_url, base)}


@app.get(publish_tiktok.CALLBACK_PATH)
def tiktok_callback(request: Request):
    res = publish_tiktok.handle_callback(dict(request.query_params))
    ok = res == "ok"
    msg = "TikTok conectado! Pode fechar esta aba e voltar ao painel." if ok else res
    return HTMLResponse(f"<!doctype html><meta charset=utf-8><title>TikTok</title>"
                        f"<body style='font:18px system-ui;padding:40px;max-width:640px'>{msg}</body>")


@app.post("/api/accounts/tiktok/disconnect")
def tiktok_disconnect():
    publish_tiktok.disconnect()
    return {"ok": True}


@app.get("/media/{job_id}/{filename}")
def media(job_id: str, filename: str, download: int = 0):
    path = (JOBS_DIR / job_id / "clips" / filename).resolve()
    if JOBS_DIR.resolve() not in path.parents or not path.is_file():
        raise HTTPException(404, "Arquivo não encontrado")
    if download:
        return FileResponse(path, filename=filename)
    return FileResponse(path)


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")


@app.middleware("http")
async def _no_stale_panel(request: Request, call_next):
    """O navegador sempre confere se o painel mudou (senão continua mostrando a versão antiga depois de
    uma atualização, mesmo com F5)."""
    resp = await call_next(request)
    if request.url.path.startswith("/web/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


def _free_port(start: int) -> int:
    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex((HOST, port)) != 0:
                return port
    return start


def main() -> None:
    import uvicorn
    port = _free_port(PORT)
    url = f"http://{HOST}:{port}"
    print(f"\n  Painel de cortes: {url}\n  (deixe esta janela aberta enquanto usa)\n")
    if os.environ.get("NO_BROWSER") != "1":
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=HOST, port=port, log_level="warning")


if __name__ == "__main__":
    main()
