"""Aprender com as views: lê os números dos Shorts publicados e descobre o que rende mais.

- Busca views, curtidas e comentários de cada Short enviado (API do YouTube, permissão de leitura que a conta
  já tem). Atualiza a cada 3 horas.
- Compara grupos pela mediana de views: streamer, estilo do título (nome no começo, pergunta, cita o GTA 6),
  horário de publicação e enquadramento. Só conta vídeos com mais de 36 h (antes disso os números ainda mudam).
- Usa o resultado em três lugares:
  1. nota dos cortes novos (streamer e enquadramento que rendem mais ganham pontos; os que rendem menos perdem),
  2. dica de estilo de título enviada para a IA,
  3. painel "O que está rendendo" na Agenda.
"""
from __future__ import annotations

import json
import math
import re
import statistics
import time
from datetime import datetime

from .config import DATA_DIR, JOBS_DIR

INSIGHTS = DATA_DIR / "insights.json"
REFRESH = 3 * 3600
MIN_AGE = 24 * 3600     # antes disso as views de um Short ainda estão chegando
MIN_POSTS = 8           # abaixo disso é sorte, não padrão
MIN_GROUP = 3
GTA = re.compile(r"\bgta\b|gta ?(6|vi)\b", re.IGNORECASE)


# ---------------------------------------------------------------- coleta
def refresh_stats(posts: list[dict], force: bool = False) -> int:
    """Atualiza p["stats"] dos Shorts publicados. Retorna quantos foram atualizados."""
    from .publish_youtube import TOKEN, _client
    if not TOKEN.exists():
        return 0
    now = time.time()
    todo = [p for p in posts if p["platform"] == "youtube" and p["status"] == "done"
            and (p.get("result") or {}).get("id")
            and now - _published_at(p) < 45 * 86400
            and (force or now - (p.get("stats") or {}).get("checked_at", 0) > REFRESH)]
    if not todo:
        return 0
    yt = _client()
    n = 0
    for i in range(0, len(todo), 50):
        batch = todo[i:i + 50]
        res = yt.videos().list(part="statistics,status", id=",".join(p["result"]["id"] for p in batch)).execute()
        by_id = {it["id"]: it for it in res.get("items", [])}
        for p in batch:
            it = by_id.get(p["result"]["id"])
            if not it:
                p["stats"] = {"missing": True, "checked_at": now}  # apagado ou privado
                continue
            st = it.get("statistics", {})
            old = p.get("stats") or {}
            p["stats"] = {"views": int(st.get("viewCount", 0)), "likes": int(st.get("likeCount", 0)),
                          "comments": int(st.get("commentCount", 0)),
                          "public": it.get("status", {}).get("privacyStatus") == "public", "checked_at": now,
                          **{k: old[k] for k in ("retention", "avg_seconds") if k in old}}
            n += 1
    try:
        _refresh_retention(todo)
    except Exception as exc:
        print(f"[aprender] retenção não lida: {exc}")
    return n


def _refresh_retention(posts: list[dict]) -> None:
    """Retenção pelo YouTube Analytics (precisa da permissão; o painel pede para reconectar).
    averageViewPercentage nos Shorts passa de 100% quando as pessoas assistem de novo (loop)."""
    from datetime import date, timedelta
    from .publish_youtube import analytics_client
    yta = analytics_client()
    if not yta or not posts:
        return
    first = min(_published_at(p) for p in posts)
    start = (datetime.fromtimestamp(first).date() - timedelta(days=1)).isoformat()
    by_id = {p["result"]["id"]: p for p in posts}
    ids = list(by_id)
    for i in range(0, len(ids), 200):
        res = yta.reports().query(ids="channel==MINE", startDate=start, endDate=date.today().isoformat(),
                                  metrics="views,averageViewDuration,averageViewPercentage",
                                  dimensions="video", filters="video==" + ",".join(ids[i:i + 200])).execute()
        for row in res.get("rows") or []:
            p = by_id.get(row[0])
            if p and p.get("stats") is not None:
                p["stats"]["avg_seconds"] = round(float(row[2]), 1)
                p["stats"]["retention"] = round(float(row[3]), 1)


def _published_at(p: dict) -> float:
    return float(p.get("scheduled_at") or p.get("sent_at") or p.get("created_at") or 0)


_layouts: dict[tuple, str] = {}


def _layout(p: dict) -> str:
    if p.get("layout"):
        return p["layout"]
    key = (p["job_id"], p.get("clip_index"))
    if key not in _layouts:
        try:
            job = json.loads((JOBS_DIR / p["job_id"] / "job.json").read_text(encoding="utf-8"))
            _layouts[key] = job["clips"][p["clip_index"]].get("layout_used") or "?"
        except Exception:
            _layouts[key] = "?"
    return _layouts[key]


LAYOUT_NAMES = {"split": "Webcam + jogo", "face": "Seguindo o rosto", "blur": "Fundo desfocado",
                "vertical": "Vertical original", "?": "Desconhecido"}


def features(p: dict) -> dict[str, str]:
    title = p.get("title") or ""
    name = (p.get("streamer") or "").lower()
    first = title.split()[0].lower().strip(",.!?:") if title.split() else ""
    return {
        "streamer": p.get("streamer") or "?",
        "titulo_nome": "Nome do streamer no começo" if name and first == name else "Situação no começo",
        "titulo_pergunta": "Título com pergunta" if "?" in title else "Título sem pergunta",
        "titulo_gta": "Cita o GTA 6 no título" if GTA.search(title) else "Não cita o GTA 6",
        "horario": datetime.fromtimestamp(_published_at(p)).strftime("%Hh"),
        "layout": LAYOUT_NAMES.get(_layout(p), _layout(p)),
    }


# ---------------------------------------------------------------- análise
GROUP_NAMES = {"streamer": "Streamer", "titulo_nome": "Começo do título", "titulo_pergunta": "Pergunta no título",
               "titulo_gta": "GTA 6 no título", "horario": "Horário", "layout": "Enquadramento"}


def analyze(posts: list[dict]) -> dict:
    now = time.time()
    rows = [p for p in posts if p["platform"] == "youtube" and p["status"] == "done"
            and (p.get("stats") or {}).get("views") is not None and now - _published_at(p) >= MIN_AGE]
    out = {"updated_at": now, "measured": len(rows), "need": MIN_POSTS, "ready": len(rows) >= MIN_POSTS,
           "groups": {}, "findings": [], "title_hint": "", "top": []}
    measured_all = [p for p in posts if (p.get("stats") or {}).get("views") is not None]
    out["top"] = [{"title": p["title"], "views": p["stats"]["views"], "url": (p.get("result") or {}).get("url"),
                   "retention": p["stats"].get("retention")}
                  for p in sorted(measured_all, key=lambda x: -x["stats"]["views"])[:5]]
    if not rows:
        return out
    overall = max(1.0, statistics.median(p["stats"]["views"] for p in rows))
    out["median_views"] = overall
    feats = [(features(p), p["stats"]["views"]) for p in rows]
    for g in GROUP_NAMES:
        vals: dict[str, list[int]] = {}
        for f, v in feats:
            vals.setdefault(f[g], []).append(v)
        out["groups"][g] = {k: {"n": len(v), "median": statistics.median(v),
                                "factor": round(max(1.0, statistics.median(v)) / overall, 2)}
                            for k, v in vals.items()}
    if not out["ready"]:
        return out

    # o que se destaca (grupos com amostra mínima), do mais forte para o mais fraco
    findings = []
    for g, vals in out["groups"].items():
        ok = {k: v for k, v in vals.items() if v["n"] >= MIN_GROUP and k not in ("Desconhecido", "?")}
        if len(ok) < 2:
            continue
        best = max(ok.items(), key=lambda kv: kv[1]["median"])
        worst = min(ok.items(), key=lambda kv: kv[1]["median"])
        ratio = max(1.0, best[1]["median"]) / max(1.0, worst[1]["median"])
        if ratio >= 1.5:
            findings.append({"group": GROUP_NAMES[g], "best": best[0], "worst": worst[0], "ratio": round(ratio, 1),
                             "text": f"{GROUP_NAMES[g]}: “{best[0]}” rende {ratio:.1f}x mais que “{worst[0]}” "
                                     f"(mediana {best[1]['median']:.0f} x {worst[1]['median']:.0f} views)"})
    # retenção (quanto do Short as pessoas assistem): o sinal que o algoritmo mais usa
    ret_rows = [(f, p["stats"]["retention"]) for (f, _), p in zip(feats, rows) if p["stats"].get("retention") is not None]
    if len(ret_rows) >= MIN_POSTS:
        out["median_retention"] = statistics.median(r for _, r in ret_rows)
        for g in ("titulo_nome", "titulo_pergunta", "titulo_gta", "layout", "horario", "streamer"):
            vals: dict[str, list[float]] = {}
            for f, r in ret_rows:
                vals.setdefault(f[g], []).append(r)
            ok = {k: statistics.median(v) for k, v in vals.items()
                  if len(v) >= MIN_GROUP and k not in ("Desconhecido", "?")}
            if len(ok) >= 2:
                best, worst = max(ok.items(), key=lambda kv: kv[1]), min(ok.items(), key=lambda kv: kv[1])
                if best[1] - worst[1] >= 10:  # 10 pontos de retenção é muita diferença em Shorts
                    findings.append({"group": GROUP_NAMES[g], "best": best[0], "worst": worst[0],
                                     "ratio": round(best[1] / max(worst[1], 1), 1),
                                     "text": f"Retenção — {GROUP_NAMES[g]}: “{best[0]}” segura {best[1]:.0f}% do vídeo, "
                                             f"“{worst[0]}” só {worst[1]:.0f}%"})
    out["findings"] = sorted(findings, key=lambda x: -x["ratio"])

    hints = []
    for fnd in out["findings"]:
        if fnd["best"] == "Situação no começo":
            hints.append("Comece o título pela situação, nunca pelo nome do streamer.")
        elif fnd["best"] == "Nome do streamer no começo":
            hints.append("Comece o título pelo nome do streamer.")
        elif fnd["best"] == "Título com pergunta":
            hints.append("Quando couber, escreva o título como pergunta.")
        elif fnd["best"] == "Cita o GTA 6 no título":
            hints.append("Cite o GTA 6 no título sempre que fizer sentido.")
    if hints:
        out["title_hint"] = "O QUE ESTÁ RENDENDO MAIS NO CANAL (siga): " + " ".join(hints)
    return out


def save(insights: dict) -> None:
    INSIGHTS.write_text(json.dumps(insights, ensure_ascii=False, indent=1), encoding="utf-8")


def load() -> dict:
    try:
        return json.loads(INSIGHTS.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ---------------------------------------------------------------- horários
SLOT_WINDOW = (7, 23)  # só sugere horários entre 7h e 23h
# horários ainda não testados são experimentados nesta ordem (noite primeiro: é quando o público de lives está on)
EXPLORE_ORDER = [21, 20, 22, 19, 18, 12, 13, 17, 23, 16, 14, 11, 15, 10, 9, 8, 7]


def adjust_slots(ins: dict, slots: list[str]) -> tuple[list[str], str] | None:
    """Troca no máximo UM horário: o pior já testado (3+ vídeos) que rende menos de 60% da mediana do canal,
    pelo melhor horário testado acima da mediana ou, se não houver, por um horário ainda não testado.
    Conservador de propósito: com poucos vídeos, horário e qualidade do corte se misturam."""
    if not ins.get("ready") or not slots:
        return None
    stats = {}
    for k, v in ins.get("groups", {}).get("horario", {}).items():
        if k[:-1].isdigit():
            stats[int(k[:-1])] = v
    overall = float(ins.get("median_views") or 1)
    by_hour: dict[int, str] = {}
    for s in sorted(slots):
        try:
            by_hour.setdefault(int(s.split(":")[0]), s)
        except ValueError:
            continue
    tested = [(stats[h]["median"], h) for h in by_hour if h in stats and stats[h]["n"] >= MIN_GROUP]
    if not tested:
        return None
    worst_med, worst_h = min(tested)
    if worst_med >= 0.6 * overall:
        return None  # nenhum horário atual está claramente ruim
    better = sorted(((v["median"], h) for h, v in stats.items()
                     if v["n"] >= MIN_GROUP and h not in by_hour and SLOT_WINDOW[0] <= h <= SLOT_WINDOW[1]),
                    reverse=True)
    if better and better[0][0] > overall:
        new_h, why = better[0][1], f"mediana de {better[0][0]:.0f} views lá"
    else:
        new_h = next((h for h in EXPLORE_ORDER if h not in by_hour and stats.get(h, {}).get("n", 0) < MIN_GROUP),
                     None)
        why = "horário ainda não testado, para o robô aprender"
        if new_h is None:
            return None
    new = sorted([s for s in slots if s != by_hour[worst_h]] + [f"{new_h:02d}:00"])
    note = (f"Troquei {by_hour[worst_h]} (mediana de {worst_med:.0f} views, a do canal é {overall:.0f}) "
            f"por {new_h:02d}:00 ({why}).")
    return new, note


# ---------------------------------------------------------------- uso
def title_hint() -> str:
    return load().get("title_hint", "")


def score_bonus(streamer: str, layout: str) -> tuple[int, str]:
    """Pontos extras (ou a menos) para um corte novo, pelo que já rendeu no canal."""
    ins = load()
    if not ins.get("ready"):
        return 0, ""
    bonus, notes = 0.0, []
    for g, key in (("streamer", streamer), ("layout", LAYOUT_NAMES.get(layout, layout))):
        info = ins.get("groups", {}).get(g, {}).get(key)
        if info and info["n"] >= MIN_GROUP:
            b = max(-12.0, min(12.0, 8 * math.log2(max(info["factor"], 0.05))))
            if abs(b) >= 2:
                bonus += b
                notes.append(f"{key} rende {info['factor']:.1f}x a mediana do canal")
    return int(round(bonus)), "; ".join(notes)
