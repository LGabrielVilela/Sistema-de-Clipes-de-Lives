"""Fila de jobs: baixar/gravar → transcrever → achar momentos → renderizar cortes.

Tipos de job:
- video : link de vídeo gravado (VOD)
- live  : sessão de gravação; grava em blocos e cria um job "parte" para cada bloco
- parte : um bloco da live, processado enquanto a gravação continua
"""
from __future__ import annotations

import io
import json
import queue
import shutil
import threading
import time
import traceback
import uuid
import zipfile
from pathlib import Path

import numpy as np

from . import chat as audience
from . import downloader, highlights, signals, transcriber
from .config import JOBS_DIR, WHISPER_LANGUAGE, json_default
from .media import duration
from .render import post_caption, render_clip

DEFAULT_SETTINGS = {
    "num_clips": 5, "min_len": 27, "max_len": 33, "layout": "auto",
    "language": WHISPER_LANGUAGE, "live_minutes": 120, "block_minutes": 15,
    "title_overlay": True, "section_start": None, "section_end": None,
    "cold_open": True,  # abre o vídeo com 2-3 s do clímax
    "loop_end": True,   # final emendado no começo (replay)
    "min_score": 55,    # live: abaixo disso (ou abaixo do normal do streamer) o momento não vira corte
}
ACTIVE = ("queued", "running")


class Cancelled(Exception):
    pass


def _fmt(t: float) -> str:
    h, rem = divmod(int(t), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class JobStore:
    def __init__(self, start_worker: bool = True) -> None:
        self._lock = threading.RLock()
        self._jobs: dict[str, dict] = {}
        self._stops: dict[str, threading.Event] = {}
        self._cancels: dict[str, threading.Event] = {}
        self._queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self.on_clips_ready = None  # chamado com (job, transcrição) quando os cortes ficam prontos
        self._to_recover: list[str] = []
        self._to_requeue: list[str] = []
        self._load_existing()
        if start_worker:
            for jid in self._to_requeue:
                self._queue.put((jid, "transcribe"))
            threading.Thread(target=lambda: [self._recover_session(s) for s in self._to_recover],
                             daemon=True).start()
            threading.Thread(target=self._worker, daemon=True).start()

    # ------------------------------------------------------------ persistência
    def _load_existing(self) -> None:
        for f in JOBS_DIR.glob("*/job.json"):
            try:
                job = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if job.get("status") in ACTIVE:
                if job.get("mode") == "live":
                    job["status"] = "error"
                    job["message"] = "A gravação foi interrompida porque o programa foi fechado."
                    self._to_recover.append(job["id"])
                elif (f.parent / "source.mp4").exists():
                    job.update(status="queued", step="transcribe", progress=0.0,
                               message="Retomando depois que o programa foi reaberto")
                    self._to_requeue.append(job["id"])
                else:
                    job["status"] = "error"
                    job["message"] = "Interrompido porque o programa foi fechado. Clique em “Tentar de novo”."
            self._jobs[job["id"]] = job

    def _save(self, job: dict) -> None:
        d = JOBS_DIR / job["id"]
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "job.json.tmp"
        tmp.write_text(json.dumps(job, ensure_ascii=False, indent=1, default=json_default), encoding="utf-8")
        for _ in range(10):  # o Windows às vezes trava o arquivo por instantes
            try:
                tmp.replace(d / "job.json")
                return
            except PermissionError:
                time.sleep(0.1)

    def update(self, job_id: str, **fields) -> dict:
        with self._lock:
            job = self._jobs[job_id]
            # só tipos simples do Python na memória: um número/bool do NumPy derruba a resposta da API (erro 500)
            job.update(json.loads(json.dumps(fields, default=json_default)))
            job["updated_at"] = time.time()
            self._save(job)
            return job

    # ------------------------------------------------------------ consultas
    def list(self) -> list[dict]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j["created_at"], reverse=True)
            return [{k: v for k, v in j.items() if k != "timeline"} for j in jobs]

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job and job.get("mode") == "live":
                job = {**job, "parts_info": [self._part_summary(pid) for pid in job.get("parts", [])]}
            return job

    def _part_summary(self, pid: str) -> dict:
        p = self._jobs.get(pid)
        if not p:
            return {"id": pid, "status": "deleted"}
        return {"id": pid, "title": p["title"], "status": p["status"], "step": p["step"],
                "message": p["message"], "clips": len(p["clips"]), "range": p.get("range")}

    # ------------------------------------------------------------ criação
    def _new(self, url: str, mode: str, settings: dict, **extra) -> dict:
        job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        job = {
            "id": job_id, "url": url.strip(), "mode": mode,
            "settings": {**DEFAULT_SETTINGS, **settings},
            "status": "queued", "step": "download", "progress": 0.0,
            "message": "Na fila", "title": "", "channel": "", "duration": 0, "clips": [], "timeline": [],
            "used_ai": None, "created_at": time.time(), "updated_at": time.time(), **extra,
        }
        with self._lock:
            self._jobs[job_id] = job
            self._save(job)
        return job

    def create(self, url: str, mode: str, settings: dict, **extra) -> dict:
        job = self._new(url, mode, settings, **extra)
        if mode == "live":
            self._start_live(job["id"])
        else:
            self._queue.put((job["id"], "download"))
        return job

    # ------------------------------------------------------------ ações
    def reanalyze(self, job_id: str, settings: dict) -> dict:
        job = self.get(job_id)
        if not job:
            raise KeyError(job_id)
        if job["mode"] == "live":
            raise RuntimeError("Abra uma das partes da live para gerar novos cortes.")
        if job["status"] in ACTIVE:
            raise RuntimeError("Este vídeo ainda está sendo processado.")
        if not (JOBS_DIR / job_id / "transcript.json").exists():
            raise RuntimeError("Ainda não existe transcrição para este vídeo. Use “Tentar de novo”.")
        self._need_source(job_id)
        keep = {k: job["settings"].get(k) for k in ("section_start", "section_end")}
        job = self.update(job_id, settings={**job["settings"], **settings, **keep}, status="queued",
                          step="analyze", progress=0.0, message="Na fila para gerar novos cortes")
        self._queue.put((job_id, "analyze"))
        return job

    def retry(self, job_id: str) -> dict:
        job = self.get(job_id)
        if not job:
            raise KeyError(job_id)
        if job["status"] in ACTIVE:
            raise RuntimeError("Este vídeo já está sendo processado.")
        if job["mode"] == "live":
            raise RuntimeError("Uma live interrompida não pode ser retomada. Envie o link de novo.")
        has_source = (JOBS_DIR / job_id / "source.mp4").exists()
        if not has_source and job["mode"] == "parte":
            self._need_source(job_id)
        step = "transcribe" if has_source else "download"
        job = self.update(job_id, status="queued", step=step, progress=0.0, message="Na fila")
        self._queue.put((job_id, step))
        return job

    def cancel(self, job_id: str) -> None:
        job = self.get(job_id)
        if not job:
            raise KeyError(job_id)
        if job["mode"] == "live":
            self.stop_live(job_id)
            return
        if job["status"] == "queued":
            self.update(job_id, status="canceled", message="Cancelado")
        ev = self._cancels.get(job_id)
        if ev:
            ev.set()

    def stop_live(self, job_id: str) -> None:
        ev = self._stops.get(job_id)
        if ev:
            ev.set()

    def delete(self, job_id: str) -> None:
        job = self.get(job_id)
        if not job:
            return
        if job["status"] in ACTIVE:
            raise RuntimeError("Cancele ou pare a gravação antes de excluir.")
        ids = [job_id]
        if job["mode"] == "live":
            for pid in job.get("parts", []):
                p = self._jobs.get(pid)
                if p and p["status"] in ACTIVE:
                    raise RuntimeError("Ainda há partes desta live sendo processadas.")
                ids.append(pid)
        with self._lock:
            for i in ids:
                self._jobs.pop(i, None)
        for i in ids:
            shutil.rmtree(JOBS_DIR / i, ignore_errors=True)

    def cleanup(self, protected: set[str], dry_run: bool = True) -> dict:
        """Remove da lista os vídeos que já terminaram. Preserva o que está gravando/processando e os
        vídeos com posts pendentes ou agendados (`protected`)."""
        def size(job_id: str) -> int:
            return sum(f.stat().st_size for f in (JOBS_DIR / job_id).rglob("*") if f.is_file())

        with self._lock:
            jobs = dict(self._jobs)
        remove, kept_active, kept_posts = [], 0, 0
        for j in jobs.values():
            if j["mode"] == "parte" and j.get("parent") in jobs:
                continue  # as partes saem junto com a sessão da live
            family = [j["id"]] + [pid for pid in j.get("parts", []) if pid in jobs]
            if any(jobs[f]["status"] in ACTIVE for f in family):
                kept_active += 1
            elif any(f in protected for f in family):
                kept_posts += 1
            else:
                remove.append(family)
        freed = sum(size(f) for fam in remove for f in fam)
        if not dry_run:
            with self._lock:
                for fam in remove:
                    for f in fam:
                        self._jobs.pop(f, None)
            for fam in remove:
                for f in fam:
                    shutil.rmtree(JOBS_DIR / f, ignore_errors=True)
        return {"videos": len(remove), "bytes": freed, "kept_active": kept_active, "kept_posts": kept_posts}

    # ------------------------------------------------------------ limpeza automática do disco
    SOURCE_KEEP_H = 6        # o vídeo original (arquivo grande) fica 6 h depois de cortado
    EMPTY_PART_KEEP_H = 24   # parte de live sem nenhum corte some depois de 24 h
    FAMILY_KEEP_D = 7        # vídeos/lives sem post pendente nem enviado recente somem depois de 7 dias
    MIN_FREE_GB = 15         # abaixo disso, apaga os originais mais antigos mesmo antes das 6 h

    def auto_cleanup(self, protected: set[str]) -> dict:
        """Libera disco sem perder nada que ainda vai ser usado. `protected`: jobs com post pendente,
        agendado, enviando ou enviado nos últimos 7 dias (os cortes desses ficam)."""
        now = time.time()
        freed = 0
        with self._lock:
            jobs = dict(self._jobs)
        finished = [j for j in jobs.values() if j["status"] not in ACTIVE and j["mode"] != "live"]

        def drop_source(j: dict) -> int:
            src = JOBS_DIR / j["id"] / "source.mp4"
            if not src.exists():
                return 0
            n = src.stat().st_size
            try:
                src.unlink()
            except OSError:
                return 0  # em uso (assistindo no painel?): fica para a próxima
            self.update(j["id"], source_deleted=True)
            return n

        # 1) original de quem já foi cortado há mais de 6 h
        for j in finished:
            if now - j.get("updated_at", now) > self.SOURCE_KEEP_H * 3600:
                freed += drop_source(j)
        # 2) disco apertado: apaga originais dos mais antigos primeiro, mesmo recentes
        if shutil.disk_usage(JOBS_DIR).free < self.MIN_FREE_GB * 1024 ** 3:
            for j in sorted(finished, key=lambda x: x.get("updated_at", 0)):
                if shutil.disk_usage(JOBS_DIR).free >= (self.MIN_FREE_GB + 10) * 1024 ** 3:
                    break
                freed += drop_source(j)
        # 3) partes de live que não renderam nenhum corte
        removed = 0
        for j in finished:
            if (j["mode"] == "parte" and not j.get("clips") and j["id"] not in protected
                    and now - j.get("updated_at", now) > self.EMPTY_PART_KEEP_H * 3600):
                freed += sum(f.stat().st_size for f in (JOBS_DIR / j["id"]).rglob("*") if f.is_file())
                with self._lock:
                    self._jobs.pop(j["id"], None)
                    parent = self._jobs.get(j.get("parent") or "")
                    if parent:
                        parent["parts"] = [p for p in parent.get("parts", []) if p != j["id"]]
                        self._save(parent)
                shutil.rmtree(JOBS_DIR / j["id"], ignore_errors=True)
                removed += 1
        # 4) vídeos e lives antigos, sem nada pendente nem enviado recente
        with self._lock:
            jobs = dict(self._jobs)
        for j in jobs.values():
            if j["mode"] == "parte" and j.get("parent") in jobs:
                continue  # sai junto com a sessão
            family = [j["id"]] + [p for p in j.get("parts", []) if p in jobs]
            if any(jobs[f]["status"] in ACTIVE for f in family) or any(f in protected for f in family):
                continue
            if now - max(jobs[f].get("updated_at", now) for f in family) < self.FAMILY_KEEP_D * 86400:
                continue
            for f in family:
                freed += sum(x.stat().st_size for x in (JOBS_DIR / f).rglob("*") if x.is_file())
                with self._lock:
                    self._jobs.pop(f, None)
                shutil.rmtree(JOBS_DIR / f, ignore_errors=True)
                removed += 1
        if freed:
            print(f"[limpeza] {freed / 1024 ** 3:.1f} GB liberados ({removed} item(ns) removidos)")
        return {"bytes": freed, "removed": removed}

    def _need_source(self, job_id: str) -> None:
        if not (JOBS_DIR / job_id / "source.mp4").exists():
            raise RuntimeError("O vídeo original deste item já foi apagado para liberar espaço no disco "
                               "(a limpeza automática guarda o original por 6 h depois do corte). "
                               "Os cortes prontos continuam disponíveis.")

    def rerender_clip(self, job_id: str, index: int, start: float, end: float, title: str | None) -> dict:
        job = self.get(job_id)
        if not job or index >= len(job["clips"]):
            raise KeyError("Corte não encontrado")
        if job["status"] in ACTIVE:
            raise RuntimeError("Aguarde o processamento terminar.")
        self._need_source(job_id)
        jd = JOBS_DIR / job_id
        transcript = json.loads((jd / "transcript.json").read_text(encoding="utf-8"))
        clip = dict(job["clips"][index])
        clip["start"], clip["end"] = max(0.0, start), min(end, job["duration"] or end)
        if title:
            clip["title"] = title.strip()
        if clip["end"] - clip["start"] < 3:
            raise RuntimeError("O corte precisa ter pelo menos 3 segundos.")
        s = job["settings"]
        overlay = s.get("title_overlay", True) and (bool(job.get("used_ai")) or bool(title))
        files = render_clip(jd / "source.mp4", transcript, clip, jd / "clips", clip["name"], s["layout"], overlay,
                            self._known_cam(job_id), locked=bool(self._fixed_cam(job_id)))
        if files.get("cam"):
            self._remember_cam(job_id, files["cam"])
        clip.update(files, version=clip.get("version", 0) + 1)
        with self._lock:
            clips = list(self._jobs[job_id]["clips"])
            clips[index] = clip
            return self.update(job_id, clips=clips)

    def zip_clips(self, job_id: str) -> tuple[bytes, str]:
        job = self.get(job_id)
        if not job:
            raise KeyError(job_id)
        jobs = [self._jobs[p] for p in job.get("parts", []) if p in self._jobs] if job["mode"] == "live" else [job]
        buf = io.BytesIO()
        count = 0
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:  # mp4 já é comprimido
            for j in jobs:
                prefix = f"parte_{j.get('part_index', 0) + 1:02d}_" if job["mode"] == "live" else ""
                for c in j["clips"]:
                    mp4 = JOBS_DIR / j["id"] / "clips" / c["file"]
                    if mp4.exists():
                        zf.write(mp4, prefix + c["file"])
                        zf.writestr(prefix + c["name"] + ".txt", post_caption(c) + "\n")
                        count += 1
        if not count:
            raise RuntimeError("Ainda não há cortes prontos para baixar.")
        safe = "".join(ch for ch in (job["title"] or "cortes") if ch.isalnum() or ch in " -_")[:50].strip()
        return buf.getvalue(), f"{safe or 'cortes'}.zip"

    # ------------------------------------------------------------ moldura da webcam
    def _fixed_cam(self, job_id: str) -> list | None:
        """Webcam fixada à mão no cadastro do streamer (para layouts que a detecção automática erra)."""
        job = self._jobs.get(job_id) or {}
        if job.get("streamer_id"):
            from .store import get_config
            for st in get_config()["streamers"]:
                if st["id"] == job["streamer_id"] and st.get("cam_fixed"):
                    return st["cam_fixed"]
        return None

    def _known_cam(self, job_id: str) -> list | None:
        """Moldura da webcam já achada: neste vídeo, na sessão da live ou no cadastro do streamer."""
        job = self._jobs.get(job_id) or {}
        fixed = self._fixed_cam(job_id)
        if fixed:
            return fixed
        if job.get("cam_box"):
            return job["cam_box"]
        parent = self._jobs.get(job.get("parent") or "")
        if parent and parent.get("cam_box"):
            return parent["cam_box"]
        if job.get("streamer_id"):
            from .store import get_config
            for st in get_config()["streamers"]:
                if st["id"] == job["streamer_id"] and st.get("cam_box"):
                    return st["cam_box"]
        return None

    def _remember_cam(self, job_id: str, cam: list) -> None:
        if self._fixed_cam(job_id):
            return  # webcam fixa no cadastro: não é trocada pela detecção
        job = self._jobs.get(job_id) or {}
        self.update(job_id, cam_box=cam)
        if job.get("parent") in self._jobs:
            self.update(job["parent"], cam_box=cam)
        if job.get("streamer_id"):
            from .monitor import edit_streamer
            edit_streamer(job["streamer_id"], cam_box=cam)

    # ------------------------------------------------------------ plateia, nota mínima e repetidos (live)
    @staticmethod
    def _chat_file(job: dict) -> Path | None:
        return JOBS_DIR / job["parent"] / "chat.tsv" if job.get("parent") else None

    def _rank_live(self, job: dict, ideas: list[dict], total: float) -> list[dict]:
        """Sobe a nota do que a plateia aprovou (clips e explosão do chat), tira o que já virou corte na parte
        anterior e só deixa passar o que está acima do normal deste streamer."""
        chat_file, wall = self._chat_file(job), job.get("wall_start")
        chat = audience.chat_curve(chat_file, wall, int(total) + 1) if chat_file and wall else None
        base = audience.chat_baseline(chat_file) if chat is not None else 0.0
        tw = job.get("audience_clips") or []
        for d in ideas:
            s, e = float(d["start"]), float(d["end"])
            n = sum(1 for c in tw if s - 5 <= c["t"] <= e + 5)
            bonus, ratio = min(15, 8 * n), 0.0
            if chat is not None and base > 0:
                # média móvel de 5 s: uma mensagem solta não conta, uma enxurrada de "kkkk" sim
                win = np.convolve(chat, np.ones(5) / 5, mode="same")[int(s):int(e) + 1]
                if len(win):
                    ratio = float(np.mean(np.sort(win)[-10:])) / base  # os 10 s mais agitados x o normal
                if ratio > 2.5:
                    bonus += min(10, 3 + 4 * (ratio - 2.5))
            # chat também explode com sorteio, sub e doação: se a Groq achou o trecho fraco, a plateia não salva
            if d.get("groq_score") is not None and d["groq_score"] < 50:
                bonus *= 0.3
            d["score"] = int(min(99, round(float(d["score"]) + bonus)))
            d["audience"] = {"clips": n, "chat_x": round(ratio, 1)}
            extra = []
            if n:
                extra.append(f"{n} clip(s) feitos pela plateia")
            if ratio > 2.5:
                extra.append(f"chat {ratio:.1f}x mais agitado que o normal")
            if extra:
                d["reason"] = "; ".join(extra).capitalize() + ". " + (d.get("reason") or "")
        ideas.sort(key=lambda x: -x["score"])

        sid, off = job.get("parent"), float((job.get("range") or [0])[0])
        with self._lock:
            sess = self._jobs.get(sid) or {}
            spans, hist = list(sess.get("clip_spans", [])), list(sess.get("score_hist", []))
        # sem repetidos: as partes vizinhas dividem 45 s de gravação
        spans = [sp for sp in spans if sp[2:] != [job["id"]]]  # refazer a mesma parte não conta como repetido
        ideas = [d for d in ideas
                 if all(min(d["end"] + off, sp[1]) - max(d["start"] + off, sp[0]) <= 5 for sp in spans)]
        # nota mínima: fixa no começo; depois de alguns blocos, também acima da mediana deste streamer
        floor = float(job["settings"].get("min_score", 55))
        thr = max(floor, float(np.percentile(hist, 50))) if len(hist) >= 9 else floor
        kept = [d for d in ideas if d["score"] >= thr or d["audience"]["clips"] > 0]
        if sid in self._jobs:
            self.update(sid, score_hist=(hist + [d["score"] for d in ideas])[-200:])
        if len(kept) < len(ideas):
            print(f"[live] {job['id']}: {len(ideas) - len(kept)} momento(s) abaixo da nota {thr:.0f} descartado(s)")
        return kept

    def _remember_spans(self, job: dict, clips: list[dict]) -> None:
        sid, off = job.get("parent"), float((job.get("range") or [0])[0])
        if not clips or sid not in self._jobs:
            return
        with self._lock:
            spans = [sp for sp in self._jobs[sid].get("clip_spans", []) if sp[2:] != [job["id"]]]
            spans += [[round(c["start"] + off, 1), round(c["end"] + off, 1), job["id"]] for c in clips]
            self.update(sid, clip_spans=spans[-300:])

    # ------------------------------------------------------------ LIVE (sessão)
    def _start_live(self, session_id: str) -> None:
        stop = threading.Event()
        self._stops[session_id] = stop
        threading.Thread(target=self._live_session, args=(session_id, stop), daemon=True).start()

    def _make_part(self, sid: str, seg: Path, index: int, prev: Path | None, t_start: float) -> float:
        """Transforma um bloco gravado numa "parte" que vai para a fila de corte. Retorna a duração do bloco."""
        job = self._jobs[sid]
        inherit = {k: job.get(k) for k in ("streamer_id", "streamer", "platform", "source_channel", "auto")
                   if job.get(k) is not None}
        part = self._new(job["url"], "parte", job["settings"], parent=sid, part_index=index,
                         title=f"{job.get('title') or 'Live'} — parte {index + 1}", channel=job.get("channel", ""),
                         **inherit)
        src = downloader.build_block_source(seg, prev, JOBS_DIR / part["id"] / "source.mp4")
        dur, seg_dur = duration(src), duration(seg)
        rng = [max(0.0, t_start - (dur - seg_dur)), t_start + seg_dur]
        # horário real do 1º segundo da parte: o bloco termina quando o arquivo para de ser escrito.
        # Serve para alinhar o chat e os clips da Twitch com o vídeo.
        wall_start = seg.stat().st_mtime - dur
        self.update(part["id"], duration=round(dur, 2), range=[round(rng[0]), round(rng[1])],
                    wall_start=round(wall_start, 1), twitch=job.get("twitch"), message="Na fila para cortar")
        with self._lock:
            parts = list(self._jobs[sid].get("parts", [])) + [part["id"]]
        self.update(sid, parts=parts)
        self._queue.put((part["id"], "transcribe"))
        return seg_dur

    def _recover_session(self, sid: str) -> None:
        """Programa fechado no meio da gravação: manda para corte os blocos que ficaram no disco."""
        blocks_dir = JOBS_DIR / sid / "blocos"
        try:
            segs = sorted(blocks_dir.glob("bloco_*.ts")) if blocks_dir.exists() else []
            done_idx = {self._jobs[p].get("part_index") for p in self._jobs[sid].get("parts", []) if p in self._jobs}
            prev, t, recovered = None, 0.0, 0
            for seg in segs:
                idx = int(seg.stem.split("_")[1])
                try:
                    seg_dur = duration(seg)
                except Exception:
                    continue  # arquivo cortado ao meio sem nada aproveitável
                if idx not in done_idx and seg.stat().st_size > 200_000 and seg_dur > 20:
                    self._make_part(sid, seg, idx, prev, t)
                    recovered += 1
                prev, t = seg, t + seg_dur
            if recovered:
                self.update(sid, message=f"A gravação foi interrompida porque o programa foi fechado. "
                                         f"{recovered} trecho(s) gravado(s) foram recuperados e enviados para corte.")
        except Exception:
            traceback.print_exc()
        finally:
            shutil.rmtree(blocks_dir, ignore_errors=True)

    def _live_session(self, sid: str, stop: threading.Event) -> None:
        job = self.update(sid, status="running", step="download", message="Conectando à live")
        s = job["settings"]
        sd = JOBS_DIR / sid
        blocks_dir = sd / "blocos"
        try:
            info = downloader.get_info(job["url"])
            title = info.get("title") or "Live"
            self.update(sid, title=title, channel=info.get("uploader") or info.get("channel") or "",
                        parts=[], thumbnail=info.get("thumbnail"))
            if not downloader.is_live(info):
                raise RuntimeError("Esse link não é de uma live acontecendo agora. Use “Vídeo gravado”.")
            handle = audience.twitch_handle(job["url"])
            if handle:  # chat da Twitch gravado junto com a live (sinal de momento quente)
                audience.TwitchChat(handle, sd / "chat.tsv", stop)
                self.update(sid, twitch=handle)

            block_s = max(60, int(s["block_minutes"]) * 60)
            state = {"prev": None, "t": 0.0}  # t = tempo já gravado da live

            def on_block(seg: Path, index: int):
                state["t"] += self._make_part(sid, seg, index, state["prev"], state["t"])
                state["prev"] = seg

            n = downloader.record_live_blocks(
                job["url"], blocks_dir, int(s["live_minutes"]) * 60, block_s,
                lambda f, m: self.update(sid, progress=round(f, 3), message=m), stop, on_block)
            if n == 0:
                raise RuntimeError("Nada foi gravado. A live pode ter terminado ou estar indisponível.")
            self.update(sid, status="done", step="render", progress=1.0,
                        message=f"Gravação encerrada: {n} bloco(s). Os cortes de cada parte aparecem abaixo.")
        except Exception as exc:
            traceback.print_exc()
            self.update(sid, status="error", message=str(exc).strip()[:600])
        finally:
            self._stops.pop(sid, None)
            # os blocos .ts originais não são mais necessários (cada parte tem seu source.mp4)
            shutil.rmtree(blocks_dir, ignore_errors=True)

    # ------------------------------------------------------------ execução
    def _worker(self) -> None:
        while True:
            job_id, from_step = self._queue.get()
            job = self._jobs.get(job_id)
            if not job or job["status"] == "canceled":
                continue
            cancel = threading.Event()
            self._cancels[job_id] = cancel
            try:
                self._run(job_id, from_step, cancel)
            except (Cancelled, transcriber.Cancelled, downloader.Cancelled):
                self.update(job_id, status="canceled", message="Cancelado. Clique em “Tentar de novo” para continuar.")
            except Exception as exc:
                traceback.print_exc()
                if job_id in self._jobs:
                    self.update(job_id, status="error", message=str(exc).strip()[:600])
            finally:
                self._cancels.pop(job_id, None)

    def _progress(self, job_id: str, step: str):
        def fn(frac: float, msg: str):
            self.update(job_id, step=step, progress=round(float(frac), 3), message=msg)
        return fn

    def _run(self, job_id: str, from_step: str, cancel: threading.Event) -> None:
        def check():
            if cancel.is_set():
                raise Cancelled()

        job = self.update(job_id, status="running")
        jd = JOBS_DIR / job_id
        s = job["settings"]
        source = jd / "source.mp4"

        # 1) download
        if from_step == "download":
            p = self._progress(job_id, "download")
            p(0, "Lendo informações do link")
            info = downloader.get_info(job["url"])
            self.update(job_id, title=info.get("title") or "", thumbnail=info.get("thumbnail"),
                        channel=info.get("uploader") or info.get("channel") or "")
            if downloader.is_live(info):
                # o usuário escolheu "vídeo", mas o link é de uma live acontecendo agora
                self.update(job_id, mode="live", status="queued")
                self._start_live(job_id)
                return
            section = None
            a, b = s.get("section_start"), s.get("section_end")
            if a is not None or b is not None:
                a = float(a or 0) * 60
                b = float(b) * 60 if b else float(info.get("duration") or 10 ** 7)
                if b - a < 30:
                    raise RuntimeError("O trecho escolhido é curto demais (mínimo 30 segundos).")
                section = (a, b)
                self.update(job_id, range=[round(a), round(b)])
            source = downloader.download_vod(job["url"], jd, p, section, cancel.is_set)
            self.update(job_id, duration=round(duration(source), 2))

        check()
        job = self.get(job_id)
        if not job["duration"]:
            job = self.update(job_id, duration=round(duration(source), 2))
        total = float(job["duration"])

        # 2) transcrição
        tr_path = jd / "transcript.json"
        if tr_path.exists():
            transcript = json.loads(tr_path.read_text(encoding="utf-8"))
        else:
            transcript = transcriber.transcribe(source, tr_path, s["language"], total,
                                                self._progress(job_id, "transcribe"), cancel.is_set,
                                                streamer=job.get("channel") or job.get("streamer") or "")
        if not transcript["segments"]:
            raise RuntimeError("Não foi detectada fala neste trecho, então não há o que cortar.")
        check()

        # 3) análise
        p = self._progress(job_id, "analyze")
        p(0.05, "Medindo volume do áudio e atividade do chat")
        curve_path = jd / "curve.npy"
        if curve_path.exists():
            curve = np.load(curve_path)
        else:
            energy = signals.audio_energy(source)
            chat = signals.chat_activity(jd, len(energy))
            tw_clips: list[dict] = []
            if job.get("wall_start"):  # parte de live: chat e clips da Twitch daquele horário
                if chat is None:
                    chat = audience.chat_curve(self._chat_file(job), job["wall_start"], len(energy))
                if job.get("twitch"):
                    try:
                        tw_clips = audience.twitch_clips(job["twitch"], job["wall_start"], len(energy))
                    except Exception as exc:
                        print(f"[clips] não deu para ler os clips da Twitch: {exc}")
                self.update(job_id, audience_clips=tw_clips, has_chat=chat is not None)
            curve = signals.excitement_curve(
                energy, chat, audience.clips_curve(tw_clips, len(energy)) if tw_clips else None)
            np.save(curve_path, curve)
        peaks = signals.top_peaks(curve)
        self.update(job_id, timeline=signals.timeline_points(curve))
        check()

        p(0.3, "IA escolhendo os melhores momentos")
        used_ai, ai_error, ai_provider = True, None, None
        try:
            ideas, ai_provider = highlights.ask_ai(transcript, peaks, s, job.get("title", ""),
                                                   is_live_part=job["mode"] == "parte", curve=curve,
                                                   streamer=job.get("channel") or job.get("streamer") or "")
            p(0.5, f"{ai_provider} escolheu os momentos")
            from .moments import groq_rerank, words_between
            if ai_provider == "IA local" and len(ideas) > 1:
                p(0.52, "Groq reavaliando os finalistas")
                if groq_rerank(ideas, transcript, job.get("title", ""),
                               job.get("channel") or job.get("streamer") or "",
                               self.get(job_id).get("audience_clips"), source):
                    ai_provider = "IA local + Groq"
            for d in ideas:  # descobertas do GTA 6 (mapa, gráfico, detalhe escondido...) rendem mais no canal
                b, found = highlights.discovery_bonus(words_between(transcript, d["start"], d["end"]))
                if b:
                    d["score"] = int(min(99, int(d.get("score", 0)) + b))
                    d["reason"] = f"Curiosidade do GTA 6 ({', '.join(found[:3])}) +{b}. " + (d.get("reason") or "")
            ideas.sort(key=lambda x: -int(x.get("score", 0)))
            if job["mode"] == "parte":
                ideas = self._rank_live(self.get(job_id), ideas, total)
            clips = highlights.snap_and_clean(ideas, transcript, s, total)
            if job["mode"] == "parte":
                self._remember_spans(job, clips)
        except Exception as exc:
            used_ai, ai_error = False, highlights.friendly_error(exc)
            print(f"[ia] {ai_error}")
            clips = highlights.fallback_from_peaks(peaks, transcript, s, total)
        self.update(job_id, used_ai=used_ai, ai_error=ai_error, ai_provider=ai_provider)
        if not clips:
            if job["mode"] == "parte":
                self.update(job_id, status="done", step="render", progress=1.0, clips=[],
                            message="Nenhum momento acima do normal deste streamer neste bloco "
                                    "(ou o momento já virou corte na parte anterior).")
                return
            raise RuntimeError("Nenhum trecho bom foi encontrado. Tente diminuir a duração mínima.")
        if used_ai and ai_provider.startswith("IA local"):
            from .moments import refine_titles  # título final pela Groq (grátis), quando houver chave
            p(0.55, "Escrevendo os títulos")
            n = refine_titles(clips, transcript, job.get("title", ""), job.get("channel") or job.get("streamer") or "",
                              self.get(job_id).get("audience_clips"), source)
            if n:
                self.update(job_id, ai_provider="IA local + Groq")
                ai_provider = "IA local + Groq"
        for c in clips:
            c["loop"] = bool(s.get("loop_end", True))
        if s.get("cold_open", True):
            for c in clips:
                c["cold_open"] = highlights.pick_cold_open(c, transcript, curve, source)
        check()

        # 4) renderização
        clips_dir = jd / "clips"
        if clips_dir.exists():
            shutil.rmtree(clips_dir, ignore_errors=True)
        done = []
        known = self._known_cam(job_id)
        overlay = s.get("title_overlay", True) and used_ai  # sem IA o "título" é só o começo da fala
        for i, clip in enumerate(clips):
            check()
            self.update(job_id, step="render", progress=i / len(clips),
                        message=f"Renderizando corte {i + 1} de {len(clips)}")
            name = f"corte_{i + 1:02d}"
            files = render_clip(source, transcript, clip, clips_dir, name, s["layout"], overlay, known,
                                locked=bool(self._fixed_cam(job_id)))
            if files.get("cam"):
                known = files["cam"]
                self._remember_cam(job_id, known)
            done.append({**clip, "name": name, **files, "version": 0})
            self.update(job_id, clips=done)
        # cortes renderizados antes de a webcam ser achada: refaz no mesmo formato dos outros
        if known and s["layout"] in ("auto", "split"):
            for i, c in enumerate(done):
                if c.get("layout_used") in ("blur", "face"):
                    check()
                    self.update(job_id, message=f"Ajustando o corte {i + 1} para o formato webcam + jogo")
                    files = render_clip(source, transcript, c, clips_dir, c["name"], s["layout"], overlay, known,
                                        locked=bool(self._fixed_cam(job_id)))
                    if files.get("layout_used") == "split":
                        done[i] = {**c, **files, "version": c.get("version", 0) + 1}
                        self.update(job_id, clips=done)

        if s["layout"] == "auto":
            # fundo desfocado = webcam não encontrada; a imagem fica confusa e rende menos nos Shorts
            for c in done:
                if c.get("layout_used") == "blur" and not c.get("blur_penalty"):
                    c["score"] = max(0, int(c.get("score", 0)) - 15)
                    c["blur_penalty"] = True
                    c["reason"] = "Webcam não encontrada, saiu com fundo desfocado (-15). " + (c.get("reason") or "")
            self.update(job_id, clips=done)
        self.update(job_id, status="done", step="render", progress=1.0,
                    message=f"{len(done)} cortes prontos" + (f" ({ai_provider})" if used_ai else " (modo sem IA)"))
        if self.on_clips_ready:
            try:
                self.on_clips_ready(self.get(job_id), transcript)
            except Exception:
                traceback.print_exc()


store: JobStore | None = None


def get_store() -> JobStore:
    global store
    if store is None:
        store = JobStore()
    return store
