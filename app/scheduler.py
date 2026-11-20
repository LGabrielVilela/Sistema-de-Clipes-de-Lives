"""Fila de postagem com aprovação e agendamento (3 horários por dia por plataforma, configurável).

Estados de um post:
  pending   → aguardando sua aprovação
  scheduled → aprovado, com horário marcado
  sending   → sendo enviado agora
  done      → enviado (YouTube: agendado/publicado; TikTok: publicado ou nos rascunhos)
  failed    → falhou 3 vezes (veja o erro)
  discarded → descartado
"""
from __future__ import annotations

import json
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone

from . import learn, notify_telegram, publish_tiktok, publish_youtube, seo, trends
from .config import DATA_DIR, JOBS_DIR, json_default
from .store import get_config

POSTS_PATH = DATA_DIR / "posts.json"
PLATFORMS = ("youtube", "tiktok")
TAKEN = ("scheduled", "sending", "done")


class Scheduler:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._posts: list[dict] = self._read()
        for p in self._posts:  # envio interrompido pelo fechamento do programa
            if p["status"] == "sending":
                p["status"] = "scheduled"
            if (p.get("error") or "").startswith("O YouTube atingiu o limite"):
                p["error"] = None  # aviso antigo de limite aparecia como falha; agora é só observação
        self._load_hold()
        self._wake = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    # ------------------------------------------------------------ persistência
    def _read(self) -> list[dict]:
        try:
            return json.loads(POSTS_PATH.read_text(encoding="utf-8"))
        except Exception:
            return []

    def _save(self) -> None:
        tmp = POSTS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._posts, ensure_ascii=False, indent=1, default=json_default), encoding="utf-8")
        tmp.replace(POSTS_PATH)

    def _find(self, pid: str) -> dict:
        for p in self._posts:
            if p["id"] == pid:
                return p
        raise KeyError(pid)

    def list(self) -> list[dict]:
        with self._lock:
            return sorted(self._posts, key=lambda p: (p.get("scheduled_at") or 9e12, -p["created_at"]))

    # ------------------------------------------------------------ entrada de cortes
    def on_clips_ready(self, job: dict, transcript: dict) -> None:
        cfg = get_config()["publish"]
        platforms = [p for p in PLATFORMS if cfg[p]["enabled"]]
        if not platforms or not job.get("clips"):
            return
        # aprender com as views: streamer/enquadramento que rendem mais no canal sobem a nota (e vice-versa)
        who = job.get("streamer") or job.get("channel", "")
        for c in job["clips"]:
            if "learn_bonus" not in c:
                b, note = learn.score_bonus(who, c.get("layout_used", ""))
                c["learn_bonus"] = b
                if b:
                    c["score"] = max(0, min(99, int(c.get("score", 0)) + b))
                    c["reason"] = f"Histórico do canal: {note} ({b:+d}). " + (c.get("reason") or "")
        chosen = [(i, c) for i, c in enumerate(job["clips"]) if int(c.get("score", 0)) >= int(cfg["min_score"])]
        if not chosen:
            return
        metas = seo.build_posts(job, [c for _, c in chosen], transcript, cfg.get("credit_line", True))
        with self._lock:
            for (i, c), meta in zip(chosen, metas):
                for plat in platforms:
                    if any(p["job_id"] == job["id"] and p["clip_name"] == c["name"] and p["platform"] == plat
                           and p["status"] != "discarded" for p in self._posts):
                        continue
                    self._posts.append({
                        "id": uuid.uuid4().hex[:10], "job_id": job["id"], "clip_index": i, "clip_name": c["name"],
                        "file": c["file"], "thumb": c.get("thumb"), "score": c.get("score", 0),
                        "layout": c.get("layout_used"), "groq_score": c.get("groq_score"), "rubric": c.get("rubric"),
                        "platform": plat, "status": "pending", "streamer": job.get("streamer") or job.get("channel", ""),
                        "title": meta["youtube_title"], "description": meta["youtube_description"],
                        "tags": meta["youtube_tags"], "caption": meta["tiktok_caption"],
                        "created_at": time.time(), "scheduled_at": None, "attempts": 0,
                        "result": None, "error": None,
                    })
                    # "Postar sozinho": o corte fica na disputa; perto de cada horário o melhor é escolhido
                    # (_auto_fill), em vez de entrar numa fila por ordem de chegada
            self._save()
        self._wake.set()

    def add_clip(self, job: dict, clip_index: int, transcript: dict, platforms: list[str]) -> None:
        """Manda um corte específico para a fila (botão no painel), ignorando a nota mínima."""
        c = job["clips"][clip_index]
        meta = seo.build_posts(job, [c], transcript, get_config()["publish"].get("credit_line", True))[0]
        with self._lock:
            for plat in platforms:
                self._posts.append({
                    "id": uuid.uuid4().hex[:10], "job_id": job["id"], "clip_index": clip_index,
                    "clip_name": c["name"], "file": c["file"], "thumb": c.get("thumb"), "score": c.get("score", 0),
                    "layout": c.get("layout_used"),
                    "platform": plat, "status": "pending", "streamer": job.get("streamer") or job.get("channel", ""),
                    "title": meta["youtube_title"], "description": meta["youtube_description"],
                    "tags": meta["youtube_tags"], "caption": meta["tiktok_caption"],
                    "created_at": time.time(), "scheduled_at": None, "attempts": 0, "result": None, "error": None,
                })
            self._save()

    def backfill(self, platforms: list[str]) -> int:
        """Cria o post das plataformas ativadas para cortes que já esperam na fila de outra plataforma.
        Reaproveita o texto já gerado (título, descrição, legenda), sem gastar nova chamada de IA."""
        added = 0
        with self._lock:
            have = {(p["job_id"], p["clip_name"], p["platform"]) for p in self._posts if p["status"] != "discarded"}
            waiting = [p for p in self._posts if p["status"] in ("pending", "scheduled")]
            for plat in platforms:
                for src in waiting:
                    key = (src["job_id"], src["clip_name"], plat)
                    if src["platform"] == plat or key in have:
                        continue
                    have.add(key)
                    self._posts.append({**{k: src[k] for k in ("job_id", "clip_index", "clip_name", "file", "thumb",
                                                                "score", "streamer", "title", "description", "tags",
                                                                "caption", "layout") if k in src},
                                        "id": uuid.uuid4().hex[:10], "platform": plat, "status": "pending",
                                        "created_at": time.time(), "scheduled_at": None, "attempts": 0,
                                        "result": None, "error": None})
                    added += 1
            if added:
                self._save()
        return added

    def regenerate(self) -> int:
        """Refaz título, descrição, tags e legenda dos posts pendentes com a IA disponível (em segundo plano)."""
        with self._lock:
            if getattr(self, "_regen_running", False):
                raise RuntimeError("Os títulos já estão sendo refeitos.")
            groups: dict[tuple, list[dict]] = {}
            for p in self._posts:
                if p["status"] == "pending" or (p["status"] == "scheduled" and not p.get("result")):
                    groups.setdefault((p["job_id"], p["clip_index"]), []).append(p)
            self._regen_running = True
            self.regen_progress = {"done": 0, "total": len(groups), "provider": None}

        def work():
            import json as _json
            from .pipeline import get_store
            try:
                for (job_id, idx), posts in groups.items():
                    try:
                        job = get_store().get(job_id)
                        tr = _json.loads((JOBS_DIR / job_id / "transcript.json").read_text(encoding="utf-8"))
                        clip = job["clips"][idx]
                        meta = seo.build_posts(job, [clip], tr, get_config()["publish"].get("credit_line", True),
                                               retitle=True)[0]
                        with self._lock:
                            for p in posts:
                                # agendados que ainda não subiram também recebem o texto novo
                                if p["status"] == "pending" or (p["status"] == "scheduled" and not p.get("result")):
                                    p.update(title=meta["youtube_title"], description=meta["youtube_description"],
                                             tags=meta["youtube_tags"], caption=meta["tiktok_caption"])
                            self._save()
                    except Exception:
                        traceback.print_exc()
                    self.regen_progress["done"] += 1
            finally:
                self._regen_running = False

        threading.Thread(target=work, daemon=True).start()
        return len(groups)

    # ------------------------------------------------------------ agenda
    @staticmethod
    def _slot_list() -> list[tuple[int, int]]:
        slots = []
        for s in get_config()["publish"]["slots"]:
            try:
                h, m = (int(x) for x in s.split(":"))
                slots.append((h, m))
            except ValueError:
                continue
        return sorted(set(slots)) or [(12, 0), (18, 0), (21, 0)]

    def _slots_between(self, a: float, b: float) -> list[float]:
        day = datetime.fromtimestamp(a).astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        out = []
        for d in range(3):
            for h, m in self._slot_list():
                t = (day + timedelta(days=d, hours=h, minutes=m)).timestamp()
                if a <= t <= b:
                    out.append(t)
        return sorted(out)

    # ------------------------------------------------------------ "postar sozinho os melhores"
    PICK_LEAD = {"youtube": 45 * 60, "tiktok": 5 * 60}  # escolhe perto do horário: pega até o corte mais novo
    MAX_AGE = 48 * 3600  # corte que não foi escolhido em 48 h sai da disputa (o assunto esfriou)

    @staticmethod
    def _groq_checked(p: dict) -> bool:
        """Só entra na disputa automática quem já passou pela reavaliação da Groq (ou não dá para passar)."""
        from . import llm
        if p.get("groq_score") is not None or p.get("groq_tried") or not llm.groq_key():
            return True
        # Groq fora por muito tempo (limite diário) ou post esperando demais: entra com a nota que tem,
        # para os horários não ficarem vazios
        return llm._groq_paused_until - time.time() > 3600 or time.time() - p["created_at"] > 6 * 3600

    @staticmethod
    def _auto_rank(p: dict, now: float, last_streamer: str | None) -> tuple:
        """A NOTA decide (ela já mede o corte: qualidade, chance de viralizar, plateia, histórico do canal).
        Só em nota igual: primeiro um streamer diferente do post anterior, depois o corte mais novo."""
        other = 0 if (last_streamer and p.get("streamer") == last_streamer) else 1
        return (int(p.get("score", 0)), other, p["created_at"])

    def _auto_fill(self) -> None:
        cfg = get_config()["publish"]
        if not cfg.get("auto_approve"):
            return
        now, floor, changed = time.time(), int(cfg.get("auto_min_score", 75)), False
        with self._lock:
            for plat in PLATFORMS:
                if not cfg[plat]["enabled"]:
                    continue
                lead = self.PICK_LEAD[plat]
                for p in self._posts:
                    if p["platform"] != plat:
                        continue
                    # escolhas automáticas do modo antigo, ainda longe do horário, voltam para a disputa
                    if p["status"] == "scheduled" and p.get("auto") and not p.get("result") \
                            and p["scheduled_at"] - now > lead + 600:
                        p.update(status="pending", scheduled_at=None)
                        changed = True
                    elif p["status"] == "pending" and now - p["created_at"] > self.MAX_AGE:
                        p.update(status="discarded", expired=True)
                        changed = True
                taken = {round(p["scheduled_at"]) for p in self._posts
                         if p["platform"] == plat and p["status"] in TAKEN and p.get("scheduled_at")}
                for slot in self._slots_between(now + 120, now + lead):
                    if round(slot) in taken:
                        continue  # horário já tem post (aprovado por você ou escolhido antes)
                    pool = [p for p in self._posts if p["platform"] == plat and p["status"] == "pending"
                            and int(p.get("score", 0)) >= floor and self._groq_checked(p)]
                    if not pool:
                        break
                    before = [p for p in self._posts if p["platform"] == plat and p["status"] in TAKEN
                              and p.get("scheduled_at") and p["scheduled_at"] < slot]
                    last = max(before, key=lambda p: p["scheduled_at"]).get("streamer") if before else None
                    best = max(pool, key=lambda p: self._auto_rank(p, now, last))
                    best.update(status="scheduled", scheduled_at=slot, auto=True, attempts=0, error=None, retry_at=0)
                    taken.add(round(slot))
                    changed = True
                    print(f"[agenda] {plat} {datetime.fromtimestamp(slot):%d/%m %H:%M}: escolhido "
                          f"“{best['title']}” (nota {best.get('score')}) entre {len(pool)} cortes")
            if changed:
                self._save()

    def auto_preview(self) -> dict:
        """Para o painel: próximo horário livre de cada plataforma, quando a escolha acontece e a posição
        de cada corte na disputa."""
        cfg = get_config()["publish"]
        out = {"enabled": bool(cfg.get("auto_approve")), "floor": int(cfg.get("auto_min_score", 75)), "platforms": {}}
        if not out["enabled"]:
            return out
        now = time.time()
        with self._lock:
            for plat in PLATFORMS:
                if not cfg[plat]["enabled"]:
                    continue
                taken = {round(p["scheduled_at"]) for p in self._posts
                         if p["platform"] == plat and p["status"] in TAKEN and p.get("scheduled_at")}
                free = [t for t in self._slots_between(now + 120, now + 3 * 86400) if round(t) not in taken]
                if not free:
                    continue
                slot = free[0]
                before = [p for p in self._posts if p["platform"] == plat and p["status"] in TAKEN
                          and p.get("scheduled_at") and p["scheduled_at"] < slot]
                last = max(before, key=lambda p: p["scheduled_at"]).get("streamer") if before else None
                pool = [p for p in self._posts if p["platform"] == plat and p["status"] == "pending"
                        and int(p.get("score", 0)) >= out["floor"] and self._groq_checked(p)]
                ranked = sorted(pool, key=lambda p: self._auto_rank(p, now, last), reverse=True)
                out["platforms"][plat] = {"slot": slot, "pick_at": slot - self.PICK_LEAD[plat],
                                          "ranking": [p["id"] for p in ranked]}
        return out

    def _next_slot(self, platform: str, after: float | None = None) -> float:
        slots = self._slot_list()
        taken = {round(p["scheduled_at"]) for p in self._posts
                 if p["platform"] == platform and p["status"] in TAKEN and p.get("scheduled_at")}
        start = datetime.fromtimestamp(max(after or 0, time.time() + 300)).astimezone()
        day = start.replace(hour=0, minute=0, second=0, microsecond=0)
        for d in range(60):
            for h, m in slots:
                cand = day + timedelta(days=d, hours=h, minutes=m)
                if cand >= start and round(cand.timestamp()) not in taken:
                    return cand.timestamp()
        return start.timestamp()

    def approve(self, pid: str, fields: dict | None = None) -> dict:
        with self._lock:
            p = self._find(pid)
            if p["status"] not in ("pending", "failed"):
                raise RuntimeError("Este post já foi aprovado.")
            if fields:
                self._apply_edit(p, fields)
            p.update(status="scheduled", scheduled_at=self._next_slot(p["platform"]), attempts=0, error=None, retry_at=0)
            self._save()
        self._wake.set()
        return p

    def approve_all(self) -> int:
        n = 0
        with self._lock:
            for p in sorted(self._posts, key=lambda x: -x.get("score", 0)):  # melhores primeiro
                if p["status"] == "pending":
                    p.update(status="scheduled", scheduled_at=self._next_slot(p["platform"]), attempts=0, error=None, retry_at=0)
                    n += 1
            self._save()
        self._wake.set()
        return n

    def post_now(self, pid: str) -> dict:
        with self._lock:
            p = self._find(pid)
            if p["status"] not in ("pending", "scheduled", "failed"):
                raise RuntimeError("Este post não pode ser enviado agora.")
            p.update(status="scheduled", scheduled_at=time.time(), attempts=0, error=None, retry_at=0)
            self._save()
        self._wake.set()
        return p

    def unschedule(self, pid: str) -> dict:
        with self._lock:
            p = self._find(pid)
            if p["status"] != "scheduled":
                raise RuntimeError("Só dá para tirar da agenda posts que ainda não foram enviados.")
            p.update(status="pending", scheduled_at=None)
            self._save()
            return p

    def discard(self, pid: str) -> None:
        with self._lock:
            p = self._find(pid)
            if p["status"] == "sending":
                raise RuntimeError("Este post está sendo enviado agora.")
            p["status"] = "discarded"
            self._save()

    def edit(self, pid: str, fields: dict) -> dict:
        with self._lock:
            p = self._find(pid)
            if p["status"] in ("sending", "done"):
                raise RuntimeError("Este post já foi enviado.")
            self._apply_edit(p, fields)
            self._save()
            return p

    @staticmethod
    def _apply_edit(p: dict, fields: dict) -> None:
        for k in ("title", "description", "caption"):
            if fields.get(k) is not None:
                p[k] = str(fields[k])
        if fields.get("tags") is not None:
            p["tags"] = [t.strip().lstrip("#") for t in fields["tags"] if t.strip()]

    # ------------------------------------------------------------ envio
    def sweep_low(self) -> int:
        """Tira da Agenda os cortes cuja nota caiu abaixo da nota mínima da fila (depois da reavaliação da Groq):
        eles nunca subiriam sozinhos e só atrapalham a revisão."""
        floor = int(get_config()["publish"].get("min_score", 70))
        n = 0
        with self._lock:
            for p in self._posts:
                if p["status"] == "pending" and int(p.get("score", 0)) < floor:
                    p.update(status="discarded", low_score=True)
                    n += 1
            if n:
                self._save()
        if n:
            print(f"[agenda] {n} corte(s) com nota abaixo de {floor} saíram da Agenda")
        return n

    def rescore_pending(self, limit_jobs: int = 4) -> int:
        """Posts na disputa que não passaram pela reavaliação da Groq (criados antes dela, ou quando ela estava
        sem limite) são reavaliados aos poucos, para a escolha automática comparar notas do mesmo jeito."""
        import json as _json
        from . import llm
        from .moments import groq_rerank
        from .pipeline import get_store
        if not llm.groq_ready():
            return 0
        with self._lock:
            todo: dict[str, list[dict]] = {}
            for p in self._posts:
                if p["status"] == "pending" and p.get("groq_score") is None and not p.get("groq_tried"):
                    todo.setdefault(p["job_id"], []).append(p)
        done = 0
        for job_id, posts in list(todo.items())[:limit_jobs]:
            job = get_store().get(job_id)
            tr_path = JOBS_DIR / job_id / "transcript.json"
            if not job or not tr_path.exists():
                with self._lock:
                    for p in posts:
                        p["groq_tried"] = True
                continue
            wait = llm._groq_paused_until - time.time()
            if 0 < wait <= 120:  # limite por minuto: espera em vez de desistir
                time.sleep(wait + 1)
            tr = _json.loads(tr_path.read_text(encoding="utf-8"))
            by_clip: dict[int, dict] = {}
            for p in posts:
                if p["clip_index"] < len(job.get("clips", [])):
                    c = job["clips"][p["clip_index"]]
                    by_clip.setdefault(p["clip_index"], {"start": c["start"], "end": c["end"], "score": p["score"],
                                                         "scene": c.get("scene"), "reason": ""})
            ideas = list(by_clip.values())
            src = JOBS_DIR / job_id / "source.mp4"
            n = groq_rerank(ideas, tr, job.get("title", ""), job.get("channel") or job.get("streamer") or "",
                            job.get("audience_clips"), src if src.exists() else None, k=len(ideas)) if ideas else 0
            with self._lock:
                for idx, d in by_clip.items():
                    for p in posts:
                        if p["clip_index"] != idx or p["status"] != "pending":
                            continue
                        if "groq_score" in d:
                            p.update(score=d["score"], groq_score=d["groq_score"], rubric=d.get("rubric"))
                            done += 1
                        elif not llm.groq_ready():
                            pass  # bateu no limite: tenta de novo na próxima rodada
                        else:
                            p["groq_tried"] = True
                self._save()
        if done:
            print(f"[agenda] {done} post(s) reavaliado(s) pela Groq")
        return done

    def refresh_learning(self, force: bool = False) -> dict:
        """Lê as views dos Shorts publicados e recalcula o que está rendendo (data/insights.json)."""
        with self._lock:
            posts = list(self._posts)
        n = learn.refresh_stats(posts, force)  # rede: fora da trava, para não travar o painel
        with self._lock:
            if n:
                self._save()
            ins = learn.analyze(self._posts)
        prev = learn.load()
        ins["slot_changes"] = prev.get("slot_changes", [])
        ins["slots_changed_at"] = prev.get("slots_changed_at", 0)
        pub = get_config()["publish"]
        # horários que rendem mais: no máximo uma troca por dia (dá tempo de medir o efeito)
        if pub.get("auto_slots", True) and time.time() - ins["slots_changed_at"] > 20 * 3600:
            res = learn.adjust_slots(ins, pub["slots"])
            if res:
                new, note = res
                from .store import update_config
                update_config({"publish": {"slots": new}})
                ins["slot_changes"] = (ins["slot_changes"] + [{"at": time.time(), "note": note, "slots": new}])[-10:]
                ins["slots_changed_at"] = time.time()
                print(f"[agenda] {note}")
        learn.save(ins)
        return ins

    def _loop(self) -> None:
        last_trends = last_learn = 0.0
        while True:
            try:
                if time.time() - last_trends > 3600:
                    last_trends = time.time()
                    if publish_youtube.TOKEN.exists() or trends.get_secret("YOUTUBE_API_KEY"):
                        trends.refresh()
                if time.time() - getattr(self, "_last_rescore", 0) > 600:
                    self._last_rescore = time.time()
                    try:
                        self.rescore_pending()
                        self.sweep_low()
                    except Exception:
                        traceback.print_exc()
                if time.time() - last_learn > learn.REFRESH:
                    last_learn = time.time()
                    try:
                        self.refresh_learning()
                    except Exception:
                        traceback.print_exc()
                self._tick()
            except Exception:
                traceback.print_exc()
            self._wake.wait(20)
            self._wake.clear()

    YT_AHEAD = 6 * 3600   # YouTube: envia até 6 h antes do horário (com publishAt), não tudo na aprovação
    YT_LIMIT_WAIT = 3 * 3600

    def _due(self) -> dict | None:
        now = time.time()
        with self._lock:
            for p in sorted(self._posts, key=lambda x: x.get("scheduled_at") or 0):
                if p["status"] != "scheduled":
                    continue
                if p["platform"] == "youtube":
                    # o YouTube limita quantos vídeos o canal envia por dia: espalha os envios e respeita a pausa
                    if getattr(self, "yt_hold_until", 0) > now or p["scheduled_at"] - now > self.YT_AHEAD:
                        continue
                    if p.get("retry_at", 0) > now:
                        continue
                    p["status"] = "sending"
                    self._save()
                    return dict(p)
                if p["scheduled_at"] <= now:
                    p["status"] = "sending"
                    self._save()
                    return dict(p)
        return None

    def _tick(self) -> None:
        if getattr(self, "yt_hold_until", 0) <= time.time():  # pausa acabou: tira o aviso de espera
            with self._lock:
                stale = [p for p in self._posts if p.get("wait_note") and p["status"] == "scheduled"]
                for p in stale:
                    p["wait_note"] = None
                if stale:
                    self._save()
        try:
            self._auto_fill()
        except Exception:
            traceback.print_exc()
        while True:
            p = self._due()
            if not p:
                return
            path = JOBS_DIR / p["job_id"] / "clips" / p["file"]
            try:
                if not path.exists():
                    raise RuntimeError("O arquivo do corte não existe mais (o vídeo foi excluído?).")
                cfg = get_config()["publish"]
                if p["platform"] == "youtube":
                    when = datetime.fromtimestamp(p["scheduled_at"], tz=timezone.utc)
                    res = publish_youtube.upload(path, p["title"], p["description"], p["tags"],
                                                 cfg["youtube"].get("privacy", "public"),
                                                 when if p["scheduled_at"] > time.time() + 120 else None)
                    note = "Agendado no YouTube" if p["scheduled_at"] > time.time() + 120 else "Publicado no YouTube"
                    if res.get("privacy") == "private" and p["scheduled_at"] <= time.time() + 120:
                        note = "Enviado como privado (o app do Google ainda não passou pela auditoria)"
                else:
                    res = publish_tiktok.post(path, p["caption"], cfg["tiktok"].get("mode", "draft"))
                    note = ("Enviado para os rascunhos do TikTok — abra o app e toque em publicar"
                            if res["mode"] == "draft" else "Publicado no TikTok")
                self._finish(p["id"], status="done", result={**res, "note": note}, error=None, sent_at=time.time())
                if p["platform"] == "tiktok" and res.get("mode") == "draft":
                    self._notify(notify_telegram.draft_ready, p)
            except Exception as exc:
                traceback.print_exc()
                if p["platform"] == "youtube" and ("uploadLimitExceeded" in str(exc) or "exceeded the number" in str(exc)):
                    # limite diário do canal: não é falha do post. Pausa os envios do YouTube e tenta depois.
                    # O post MANTÉM o horário dele (se o horário passar na pausa, sobe assim que ela acabar).
                    with self._lock:
                        # o YouTube conta os envios numa janela de 24 h: a próxima vaga abre quando o envio mais
                        # antigo dessa janela completa 24 h (no mínimo 30 min, no máximo 24 h de espera)
                        now = time.time()
                        sent = [q["sent_at"] for q in self._posts if q["platform"] == "youtube"
                                and q["status"] == "done" and q.get("sent_at") and now - q["sent_at"] < 86400]
                        until = min(sent) + 86400 + 120 if sent else now + self.YT_LIMIT_WAIT
                        self._set_hold(min(max(until, now + 1800), now + 86400))
                        cur = self._find(p["id"])
                        cur.update(status="scheduled", error=None,
                                   wait_note="Esperando: o YouTube atingiu o limite de envios do dia deste canal. "
                                             "Tenta de novo às "
                                             + datetime.fromtimestamp(self.yt_hold_until).strftime("%H:%M") + ".")
                        self._save()
                    continue
                with self._lock:
                    cur = self._find(p["id"])
                    cur["attempts"] = cur.get("attempts", 0) + 1
                    cur["error"] = str(exc)[:400]
                    if cur["attempts"] >= 3:
                        cur["status"] = "failed"
                    else:  # tenta de novo em 15 minutos
                        cur["status"] = "scheduled"
                        cur["scheduled_at"] = max(cur["scheduled_at"], time.time()) + 900
                        cur["retry_at"] = time.time() + 900  # o YouTube não espera o horário: precisa da pausa
                    self._save()
                    failed = dict(cur)
                self._notify(notify_telegram.send_failed, failed, failed["status"] == "failed")

    @staticmethod
    def _notify(fn, *args) -> None:
        """Aviso no Telegram é opcional: se não estiver configurado ou falhar, não atrapalha o envio."""
        if not notify_telegram.configured():
            return
        try:
            fn(*args)
        except Exception as exc:
            print(f"[telegram] {exc}")

    def _finish(self, pid: str, **fields) -> None:
        with self._lock:
            self._find(pid).update(fields, wait_note=None)
            self._save()

    # pausa por limite do YouTube gravada em disco: reiniciar o programa não pode furar a pausa
    STATE_PATH = DATA_DIR / "scheduler_state.json"

    def _set_hold(self, until: float) -> None:
        self.yt_hold_until = until
        try:
            self.STATE_PATH.write_text(json.dumps({"yt_hold_until": until}), encoding="utf-8")
        except Exception:
            traceback.print_exc()

    def _load_hold(self) -> None:
        try:
            self.yt_hold_until = float(json.loads(self.STATE_PATH.read_text(encoding="utf-8"))["yt_hold_until"])
        except Exception:
            self.yt_hold_until = 0.0


_sched: Scheduler | None = None


def get_scheduler() -> Scheduler:
    global _sched
    if _sched is None:
        _sched = Scheduler()
    return _sched
