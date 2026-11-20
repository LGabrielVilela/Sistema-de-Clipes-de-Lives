"""Legendas animadas estilo TikTok (palavra por palavra) em formato ASS."""
from __future__ import annotations

import re
import textwrap

# Cores no formato ASS: &HAABBGGRR
WHITE = "&H00FFFFFF"
YELLOW = "&H0000C4F5"  # #F5C400
BLACK = "&H00000000"

HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{font},{size},{white},{white},{black},&H90000000,-1,0,0,0,100,100,1,0,1,7,3,2,70,70,{margin},1
Style: Title,{font},62,{white},{white},{black},&H90000000,-1,0,0,0,100,100,0,0,1,6,2,8,80,80,230,1
Style: Hook,{font},66,{white},{white},&H30000000,&H30000000,-1,0,0,0,100,100,0,0,3,22,0,8,90,90,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _ts(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


# A fonte Arial Black não tem emojis/símbolos; eles virariam quadrados no vídeo.
_NON_TEXT = re.compile(r"[^\w\s.,!?;:'\"“”…%$#@&()+\-/ºª°]", re.UNICODE)


def strip_symbols(text: str) -> str:
    return re.sub(r"\s+", " ", _NON_TEXT.sub("", text)).strip()


def _clean(word: str) -> str:
    w = strip_symbols(word).replace("{", "").replace("}", "").replace("\\", "")
    w = re.sub(r"[,;:\"“”]", "", w)
    return w.upper()


def _chunks(words: list[dict], max_words: int = 3, max_chars: int = 18) -> list[list[dict]]:
    groups: list[list[dict]] = []
    cur: list[dict] = []
    for i, w in enumerate(words):
        prev = words[i - 1] if i else None
        gap = (w["s"] - prev["e"]) if prev else 0
        text_len = sum(len(x["w"]) + 1 for x in cur) + len(w["w"])
        if cur and (len(cur) >= max_words or text_len > max_chars or gap > 0.5):
            groups.append(cur)
            cur = []
        cur.append(w)
        if re.search(r"[.!?…]$", w["w"]):
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    return groups


HOOK_SECONDS = 3.2


def hook_event(text: str, y: int, dur: float = HOOK_SECONDS) -> str:
    """Cartão de gancho: o título em caixa escura nos primeiros segundos (quem desliza sem som entende o vídeo)."""
    text = strip_symbols(text or "").replace("{", "").replace("}", "")
    if not text:
        return ""
    wrapped = "\\N".join(textwrap.wrap(text.upper(), 20)[:3])
    pop = "\\fscx80\\fscy80\\t(0,140,\\fscx104\\fscy104)\\t(140,240,\\fscx100\\fscy100)"
    return f"Dialogue: 2,{_ts(0)},{_ts(dur)},Hook,,0,0,0,,{{\\an8\\pos(540,{y})\\fad(0,250){pop}}}{wrapped}"


def hook_ass(text: str, y: int, font: str = "Arial Black") -> str:
    """Arquivo .ass só com o cartão de gancho (aplicado depois de montar o vídeo com o clímax na frente)."""
    return HEADER.format(font=font, size=84, white=WHITE, black=BLACK, margin=470) + hook_event(text, y) + "\n"


def build_ass(transcript: dict, start: float, end: float, title: str | None = None,
              margin: int = 470, font: str = "Arial Black", size: int = 84, shift: float = 0.0,
              hook: tuple[str, int] | None = None) -> str:
    """Gera o .ass com tempos relativos ao início do corte.

    shift: deslocamento (s) para compensar arquivos em que o vídeo e o áudio não começam juntos."""
    words = [
        {"w": w["w"], "s": w["s"] - start + shift, "e": w["e"] - start + shift}
        for seg in transcript["segments"] for w in seg["words"]
        if w["e"] > start and w["s"] < end and _clean(w["w"])
    ]
    out = [HEADER.format(font=font, size=size, white=WHITE, black=BLACK, margin=margin)]

    title = strip_symbols(title or "").replace("{", "").replace("}", "")
    if title:
        wrapped = "\\N".join(textwrap.wrap(title.upper(), 24)[:3])
        out.append(f"Dialogue: 1,{_ts(0)},{_ts(end - start)},Title,,0,0,0,,{{\\fad(250,0)}}{wrapped}")
    if hook:
        ev = hook_event(*hook)
        if ev:
            out.append(ev)

    groups = _chunks(words)
    for gi, group in enumerate(groups):
        next_start = groups[gi + 1][0]["s"] if gi + 1 < len(groups) else group[-1]["e"] + 0.4
        group_end = min(group[-1]["e"] + 0.35, next_start)
        for k, w in enumerate(group):
            ev_start = w["s"]
            ev_end = group[k + 1]["s"] if k + 1 < len(group) else group_end
            if ev_end <= ev_start:
                ev_end = ev_start + 0.05
            parts = []
            for j, x in enumerate(group):
                txt = _clean(x["w"])
                if j == k:
                    parts.append(f"{{\\c{YELLOW}&\\fscx112\\fscy112}}{txt}{{\\c{WHITE}&\\fscx100\\fscy100}}")
                else:
                    parts.append(txt)
            pop = "{\\t(0,80,\\fscx104\\fscy104)\\t(80,160,\\fscx100\\fscy100)}" if k == 0 else ""
            out.append(f"Dialogue: 0,{_ts(ev_start)},{_ts(ev_end)},Caption,,0,0,0,,{pop}{' '.join(parts)}")
    return "\n".join(out) + "\n"
