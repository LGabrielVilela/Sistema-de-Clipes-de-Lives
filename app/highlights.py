"""Escolha dos melhores momentos: a IA (Claude Haiku ou local) lê a transcrição + sinais de áudio/chat."""
from __future__ import annotations

import bisect
import re

import numpy as np

from pydantic import BaseModel, Field

from . import llm


class ClipIdea(BaseModel):
    start: float = Field(description="Início do corte em segundos, no vídeo original")
    end: float = Field(description="Fim do corte em segundos, no vídeo original")
    title: str = Field(description="Título curto e chamativo para TikTok/Shorts, em português")
    hook: str = Field(description="A frase de gancho que prende nos primeiros 3 segundos")
    reason: str = Field(description="Por que esse trecho funciona como corte")
    score: int = Field(description="Potencial de viralizar, de 0 a 100")
    hashtags: list[str] = Field(description="3 a 5 hashtags relevantes, sem o #")


class ClipPlan(BaseModel):
    clips: list[ClipIdea]


# Versão para a IA local: limites de tamanho impostos na geração (o Ollama respeita maxLength).
class LocalClipIdea(BaseModel):
    start: float
    end: float
    title: str = Field(max_length=70, description="Título curto de até 8 palavras. NÃO copie a fala.")
    hook: str = Field(max_length=120)
    reason: str = Field(max_length=160)
    score: int = Field(ge=0, le=100)
    hashtags: list[str] = Field(max_length=5)


class LocalClipPlan(BaseModel):
    clips: list[LocalClipIdea] = Field(max_length=6)


LOCAL_RULES = """Responda só com o JSON pedido. Regras do título:
- No máximo 8 palavras, descrevendo o que acontece de engraçado ou surpreendente no trecho.
- NUNCA copie a transcrição como título. Sem palavrões. Não invente nomes.
- hashtags: até 2 palavras simples em minúsculas sobre o assunto (ex.: "piscina").
- Exemplos de bons títulos: "Ele desmaiou o cara na piscina", "Capotou o carro e culpou o chat".
start e end são segundos que aparecem nos colchetes da transcrição. score vai de 0 a 100."""


def short_title(t: str, limit: int = 70) -> str:
    """Proteção final: título longo é cortado numa palavra inteira."""
    t = " ".join(t.split())
    if len(t) <= limit:
        return t
    cut = t[:limit].rsplit(" ", 1)[0].rstrip(",.;:!?")
    return cut + "…"


SYSTEM = """Você é um editor experiente de cortes de lives para TikTok, Reels e YouTube Shorts.
Sua tarefa é encontrar, na transcrição de uma live/vídeo, os trechos com maior chance de viralizar
como vídeo curto vertical.

Um bom corte:
- Funciona sozinho: quem nunca viu a live entende o que está acontecendo.
- Tem gancho nos primeiros 3 segundos (frase forte, reação, pergunta, polêmica, situação engraçada).
- Termina num ponto natural: a piada fechou, a história acabou, a reação aconteceu. Nunca no meio de uma frase.
- Tem emoção ou valor: humor, raiva, surpresa, conflito, opinião forte, história, informação útil, momento épico.

Evite: trechos de silêncio, leitura de doações/sub repetitiva, papo de "bom dia chat", conversa sem contexto,
e trechos que se sobrepõem a outro corte.

Os "picos de empolgação" vêm do volume do áudio e da atividade do chat. São pistas fortes, não regras:
um pico de volume pode ser só música alta.

Inclua o contexto necessário: comece um pouco antes, na fala ou situação que provocou a reação, para quem
chegar agora entender o que está acontecendo. Nunca escolha uma reação solta sem o motivo dela.

Use os tempos exatos da transcrição (em segundos). Títulos em português do Brasil, no estilo das redes
(curtos, sem clickbait mentiroso). Ordene os cortes do melhor para o pior."""


def _fmt(t: float) -> str:
    h, rem = divmod(int(t), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _sentences(transcript: dict) -> list[str]:
    """Quebra a fala em frases curtas com tempo, para a IA marcar início/fim com precisão."""
    lines, cur = [], []
    words = _all_words(transcript)
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        gap = (nxt["s"] - w["e"]) if nxt else 99
        end_sentence = w["w"].endswith((".", "!", "?", "…"))
        if end_sentence or gap > 1.0 or len(cur) >= 18:
            lines.append(f"[{cur[0]['s']:.1f}-{cur[-1]['e']:.1f}] " + " ".join(x["w"] for x in cur))
            cur = []
    return lines


def build_prompt(transcript: dict, peaks: list[dict], settings: dict, title: str) -> str:
    lines = _sentences(transcript)
    peak_txt = ", ".join(f"{p['t']}s ({_fmt(p['t'])}, força {p['score']})" for p in peaks) or "nenhum detectado"
    return (
        f"Título do vídeo original: {title or 'desconhecido'}\n\n"
        f"Quero {settings['num_clips']} cortes, cada um entre {settings['min_len']} e {settings['max_len']} "
        f"segundos.\n\n"
        f"Picos de empolgação (áudio/chat): {peak_txt}\n\n"
        f"<transcricao>\n" + "\n".join(lines) + "\n</transcricao>"
    )


LOCAL_WINDOW_CHARS = 9000  # a IA local tem memória curta: transcrições longas vão em pedaços
NL = "\n"


def _live_hint(prompt: str, is_live_part: bool) -> str:
    if not is_live_part:
        return prompt
    return ("Este é um bloco de alguns minutos de uma live em andamento. Se não houver momentos "
            "realmente bons, devolva menos cortes (ou nenhum) em vez de forçar." + NL + NL + prompt)


def _windows(lines: list[str], max_chars: int) -> list[list[str]]:
    out, cur, size = [], [], 0
    for ln in lines:
        if cur and size + len(ln) > max_chars:
            out.append(cur)
            cur, size = cur[-3:], sum(len(x) for x in cur[-3:])  # leve sobreposição entre pedaços
        cur.append(ln)
        size += len(ln)
    if cur:
        out.append(cur)
    return out


def _local_clips(transcript: dict, peaks: list[dict], settings: dict, title: str, is_live_part: bool) -> list[dict]:
    lines = _sentences(transcript)
    windows = _windows(lines, LOCAL_WINDOW_CHARS)
    per_window = max(1, -(-int(settings["num_clips"]) * 2 // len(windows))) if len(windows) > 1 else int(settings["num_clips"])
    ideas = []
    for win in windows:
        w0 = float(win[0][1:].split("-")[0]); w1 = float(win[-1][1:].split("]")[0].split("-")[1])
        local_peaks = [p for p in peaks if w0 <= p["t"] <= w1]
        sub = {**settings, "num_clips": per_window}
        peak_txt = ", ".join(f"{p['t']}s (força {p['score']})" for p in local_peaks) or "nenhum"
        prompt = _live_hint(NL.join([
            f"Título do vídeo original: {title or 'desconhecido'}",
            f"Quero até {sub['num_clips']} cortes, cada um entre {sub['min_len']} e {sub['max_len']} segundos.",
            "Use SOMENTE tempos que aparecem entre colchetes na transcrição abaixo (em segundos).",
            f"Picos de empolgação: {peak_txt}",
            "",
            "<transcricao>", *win, "</transcricao>",
        ]), is_live_part)
        plan = llm.ollama_parse(SYSTEM + NL + NL + LOCAL_RULES, prompt, LocalClipPlan, num_ctx=8192)
        for c in plan.clips:  # descarta tempos inventados, fora do pedaço
            if w0 - 5 <= c.start < c.end <= w1 + 5:
                d = c.model_dump()
                from .seo import _tag, clean_title
                d["title"] = short_title(clean_title(d["title"]) or d["title"])
                d["hashtags"] = [t for t in (_tag(h) for h in d["hashtags"]) if t][:2]
                ideas.append(d)
    ideas.sort(key=lambda c: -c["score"])
    return ideas


def ask_ai(transcript: dict, peaks: list[dict], settings: dict, title: str,
           is_live_part: bool = False, curve=None, streamer: str = "") -> tuple[list[dict], str]:
    """Escolhe os cortes com a melhor IA disponível. Retorna (cortes, nome da IA)."""
    def claude():
        prompt = _live_hint(build_prompt(transcript, peaks, settings, title), is_live_part)
        from .moments import TITLE_STYLE, with_learned
        system = with_learned(SYSTEM + "\n\nEstilo dos títulos:\n" + TITLE_STYLE)
        return [c.model_dump() for c in llm.claude_parse(system, prompt, ClipPlan, effort="medium").clips]

    def local():
        from .moments import local_clips
        from .seo import _tag, clean_title
        ideas = local_clips(transcript, curve, settings, title, streamer)
        for d in ideas:
            from .moments import strip_name_prefix
            d["title"] = short_title(clean_title(d["title"]) or d["hook"] or d["title"])
            d["title"] = strip_name_prefix(d["title"][:1].upper() + d["title"][1:], streamer)
            d["hashtags"] = [t for t in (_tag(h) for h in d["hashtags"]) if t][:2]
        return ideas

    return llm.run(claude, local)


def friendly_error(exc: Exception) -> str:
    return llm.friendly_error(exc)


# ---------- pós-processamento ----------

def _all_words(transcript: dict) -> list[dict]:
    return [w for seg in transcript["segments"] for w in seg["words"]]


def snap_and_clean(ideas: list[dict], transcript: dict, settings: dict, total: float) -> list[dict]:
    """Ajusta início/fim para não cortar palavras, respeita a duração e remove sobreposição."""
    words = _all_words(transcript)
    starts = [w["s"] for w in words]
    ends = [w["e"] for w in words]
    min_len, max_len = float(settings["min_len"]), float(settings["max_len"])

    out: list[dict] = []
    for idea in ideas:
        s, e = float(idea["start"]), float(idea["end"])
        if e <= s:
            continue
        if words:
            # começa na palavra falada naquele instante (ou na seguinte), nunca na anterior
            i = bisect.bisect_right(ends, s)
            if i < len(words):
                s = words[i]["s"]
            # termina na palavra em andamento no tempo final, nunca pega a seguinte
            j = bisect.bisect_left(starts, e) - 1
            if j >= 0:
                e = ends[j]
        prev_end = 0.0
        if words:  # o respiro antes da 1ª palavra não pode pegar o finalzinho da palavra anterior
            p = bisect.bisect_right(ends, s) - 1  # última palavra que termina até o início do corte
            prev_end = ends[p] + 0.02 if p >= 0 else 0.0
        s = max(0.0, min(prev_end, s), s - 0.15)
        e = min(total, e + 0.3)  # pouca sobra no fim: silêncio no final quebra o loop do Short
        if e - s > max_len:
            e = s + max_len
            if words:  # termina na última palavra completa dentro do limite
                j = bisect.bisect_right(ends, e) - 1
                if j >= 0 and ends[j] > s + min_len:
                    e = ends[j] + 0.3
        if e - s < min_len:
            target = s + min_len
            k = bisect.bisect_left(ends, target)
            while k < len(words) and not words[k]["w"].endswith((".", "!", "?", "…")) and ends[k] < s + max_len:
                k += 1  # anda até o fim da frase, sem passar do máximo
            e = min(total, (ends[k] + 0.4) if k < len(words) else target)
        if any(min(e, c["end"]) - max(s, c["start"]) > 3 for c in out):
            continue
        out.append({**idea, "start": round(s, 2), "end": round(e, 2)})
        if len(out) >= int(settings["num_clips"]):
            break
    return out


# Descobertas e curiosidades do GTA 6 viralizam até entre quem não conhece o streamer
# ("Mapa do GTA V no GTA VI??" foi um dos mais vistos do canal).
DISCOVERY = re.compile(
    r"\b(mapa|gr[aá]fic\w*|realis\w*|detalh\w*|segred\w*|escondid\w*|easter ?egg|f[ií]sica|"
    r"compara\w*|gta ?(5|v|cinco)|igual (ao|no) gta|textura\w*|reflexo\w*|ilumina\w*)\b",
    re.IGNORECASE)


def discovery_bonus(text: str) -> tuple[int, list[str]]:
    """Pontos extras para trechos sobre o jogo em si (mapa, gráfico, detalhe escondido, comparação…)."""
    found = list(dict.fromkeys(m.group(0).lower() for m in DISCOVERY.finditer(text or "")))
    return min(15, 6 * len(found)), found


def _norm(w: str) -> str:
    return "".join(ch for ch in w.lower() if ch.isalnum())


def _quiet_point(source, t0: float, t1: float) -> float:
    """Instante mais silencioso entre t0 e t1 (janelas de 10 ms), para o corte não pegar voz pela metade.
    A transcrição marca o fim da palavra de forma aproximada; o áudio diz onde a voz realmente acabou."""
    import subprocess
    from .config import FFMPEG
    from .media import NO_WINDOW
    if t1 - t0 < 0.03:
        return t1
    try:
        raw = subprocess.run([FFMPEG, "-v", "error", "-ss", f"{t0:.3f}", "-t", f"{t1 - t0:.3f}", "-i", str(source),
                              "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                             capture_output=True, timeout=30, creationflags=NO_WINDOW).stdout
        x = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
        n = len(x) // 160
        if n < 2:
            return t1
        rms = np.sqrt((x[:n * 160].reshape(n, 160) ** 2).mean(axis=1))
        # o mais silencioso; empate: o mais cedo (corta logo que a voz acaba)
        k = int(np.argmin(rms + np.arange(n) * 1e-3 * (rms.max() + 1)))
        return t0 + (k + 0.5) * 0.01
    except Exception:
        return t1


def pick_cold_open(clip: dict, transcript: dict, curve=None, source=None) -> list | None:
    """Trecho de ~1,5 a 3,5 s para abrir o vídeo (o clímax): a frase de gancho que a IA apontou ou, se ela não
    for achada na fala, o segundo mais quente do corte. Sempre uma frase completa, cortada no silêncio.
    Nada se o clímax já está no começo ou se não há frase completa para usar."""
    s, e = float(clip["start"]), float(clip["end"])
    words = [w for w in _all_words(transcript) if w["s"] >= s and w["e"] <= e]
    if len(words) < 6:
        return None

    ends_sentence = lambda w: w["w"].strip().endswith((".", "!", "?", "…"))

    def around(t0: float) -> list | None:
        """Janela de até 3 s perto de t0, começando no início de uma frase e terminando no fim de uma."""
        i = min(range(len(words)), key=lambda k: abs(words[k]["s"] - t0))
        if i > 0 and not ends_sentence(words[i - 1]):
            # anda até o começo da próxima frase (ou volta ao começo da atual), o que estiver mais perto
            fwd = next((k for k in range(i, len(words) - 1) if ends_sentence(words[k])), None)
            back = next((k for k in range(i - 1, -1, -1) if ends_sentence(words[k])), None)
            options = [k + 1 for k in (fwd, back) if k is not None and abs(words[k + 1]["s"] - words[i]["s"]) <= 2.0]
            if options:
                i = min(options, key=lambda k: abs(words[k]["s"] - words[i]["s"]))
        def gap_after(k: int) -> float:
            return (words[k + 1]["s"] - words[k]["e"]) if k + 1 < len(words) else 1.0

        # A abertura SEMPRE termina numa frase completa, seguida de pausa (sem pausa, o fim da palavra é
        # incerto e o corte pega a palavra pela metade). Prefere até 3,5 s; aceita até 4,5 s; senão, sem abertura.
        ends = [k for k in range(i, len(words))
                if words[k]["e"] - words[i]["s"] <= 4.5 and words[k]["e"] - words[i]["s"] >= 1.2
                and ends_sentence(words[k]) and gap_after(k) >= 0.15]
        if not ends:
            return None
        short = [k for k in ends if words[k]["e"] - words[i]["s"] <= 3.5]
        j = short[-1] if short else ends[0]
        # começo: um respiro antes da 1ª palavra, sem pegar o fim da anterior
        prev_gap = (words[i]["s"] - words[i - 1]["e"]) if i > 0 else 1.0
        a = words[i]["s"] - min(0.1, max(0.0, prev_gap - 0.02))
        # fim: o ponto mais silencioso entre o fim da palavra e o começo da próxima (ou +0,4 s)
        b_max = min(words[j]["e"] + 0.4, words[j + 1]["s"] - 0.02) if j + 1 < len(words) else words[j]["e"] + 0.4
        b = _quiet_point(source, words[j]["e"], b_max) if source else words[j]["e"] + min(0.3, gap_after(j) * 0.7)
        if b - a < 1.2 or a < s + 4:
            return None
        return [round(a, 2), round(min(b, e), 2)]

    # 1) a frase de gancho escrita pela IA, achada na fala
    hook = [_norm(x) for x in (clip.get("hook") or "").split() if _norm(x)][:6]  # o começo da frase basta
    if len(hook) >= 2:
        norm = [_norm(w["w"]) for w in words]
        best, best_i = 0.0, None
        for i in range(len(words)):
            win = norm[i:i + len(hook)]
            hit = sum(1 for x in hook if x in win) / len(hook)
            q = hit + (0.3 if norm[i] == hook[0] else 0)  # de preferência começando na 1ª palavra do gancho
            if hit >= 0.6 and q > best:
                best, best_i = q, i
        if best_i is not None:
            r = around(words[best_i]["s"])
            if r:
                return r
    # 2) o segundo mais quente (volume + chat + clips) dentro do corte
    if curve is not None and len(curve) > int(s) + 6:
        a, b = int(s) + 4, min(int(e), len(curve))
        if b > a:
            peak = a + int(np.argmax(curve[a:b]))
            return around(peak - 1.0)
    return None


def fallback_from_peaks(peaks: list[dict], transcript: dict, settings: dict, total: float) -> list[dict]:
    """Sem IA: corta em volta dos picos de empolgação."""
    target = (float(settings["min_len"]) + float(settings["max_len"])) / 2
    ranked = sorted(peaks, key=lambda p: p["score"], reverse=True)
    ideas = []
    for p in ranked:
        s = max(0.0, p["t"] - target * 0.6)
        e = min(total, s + target)
        text = " ".join(seg["text"] for seg in transcript["segments"] if seg["e"] > s and seg["s"] < e)
        first = " ".join(text.split()[:8])
        ideas.append({
            "start": s, "end": e, "title": (first + "…") if first else f"Momento em {_fmt(p['t'])}",
            "hook": first, "reason": "Pico de volume/chat (modo sem IA)",
            "score": int(min(99, 40 + p["score"] * 12)), "hashtags": ["cortes", "live"],
        })
    return snap_and_clean(ideas, transcript, settings, total)
