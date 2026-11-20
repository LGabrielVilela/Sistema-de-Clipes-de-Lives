"""Títulos, descrições e hashtags otimizados para cada plataforma (Claude + tendências)."""
from __future__ import annotations

import re

from pydantic import BaseModel, Field

from . import llm
from .trends import hashtag_pool


class ClipPost(BaseModel):
    index: int = Field(description="Número do corte, começando em 0, na ordem recebida")
    youtube_title: str = Field(description="Título do Short, até 90 caracteres, palavra-chave principal no começo")
    youtube_description: str = Field(description="2 a 4 linhas com palavras-chave naturais + 3 a 5 hashtags no fim")
    youtube_tags: list[str] = Field(description="8 a 15 tags de busca do YouTube (sem #), da mais específica à mais ampla")
    tiktok_caption: str = Field(description="Legenda do TikTok: frase curta de gancho + 4 a 6 hashtags")


class PostPlan(BaseModel):
    posts: list[ClipPost]


class LocalClipPost(BaseModel):  # IA local: só o texto; as hashtags são montadas pelo sistema
    youtube_title: str = Field(max_length=70, description="Título de até 9 palavras que descreve o momento")
    resumo: str = Field(max_length=180, description="Uma frase explicando o que acontece no corte")
    palavras_chave: list[str] = Field(max_length=2, description="Até 2 palavras simples do assunto do corte")


class LocalClipMeta(BaseModel):  # o título vem do corte; aqui só descrição e assunto
    resumo: str = Field(max_length=180, description="Uma frase explicando o que acontece no corte")
    palavras_chave: list[str] = Field(max_length=2, description="Até 2 palavras simples do assunto do corte")


LOCAL_RULES = """Responda só com o JSON pedido.
- resumo: uma frase simples dizendo o que acontece no corte, com o detalhe concreto da fala (objeto, lugar,
  ação). Comece pelo nome do streamer. Sem palavrões, sem inventar nada que não esteja na fala.
- palavras_chave: até 2 palavras comuns em minúsculas (ex.: "piscina", "perseguição")."""


SYSTEM = """Você é especialista em crescimento de canais de cortes no TikTok e no YouTube Shorts no Brasil.
Escreva títulos, descrições e hashtags para alcançar o máximo de pessoas sem enganar quem assiste.

Regras:
- Título: gancho forte e verdadeiro sobre o que acontece no corte, com a palavra-chave do nicho (ex.: GTA 6)
  perto do começo. Sem caixa alta no título inteiro e sem clickbait mentiroso.
- Hashtags: misture 1 a 2 amplas (ex.: #gta6), 2 a 3 em alta da lista recebida que combinem com o corte,
  e 1 a 2 específicas (nome do streamer, situação do corte). Nunca use hashtag sem relação com o vídeo.
- YouTube: descrição com palavras-chave que as pessoas buscam, escrita de forma natural; inclua #shorts.
- TikTok: legenda curta (até ~150 caracteres antes das hashtags); emojis com moderação são bem-vindos.
- Português do Brasil."""


def _credit(job: dict) -> str:
    src = job.get("source_channel") or job.get("url") or ""
    who = job.get("streamer") or job.get("channel") or ""
    return f"Créditos: {who} — {src}".strip(" —") if (who or src) else ""


def _fallback(job: dict, clips: list[dict], pool: dict) -> list[dict]:
    base = pool["fixas"][:3] + pool["em_alta_youtube"][:2] + pool["manuais_tiktok"][:2]
    out = []
    for i, c in enumerate(clips):
        tags = list(dict.fromkeys([t.lower() for t in base + (c.get("hashtags") or [])]))[:6]
        hs = " ".join("#" + t for t in tags)
        out.append({"index": i, "youtube_title": c["title"][:90],
                    "youtube_description": f"{c.get('hook') or c['title']}\n\n{hs} #shorts",
                    "youtube_tags": tags + pool["palavras_chave_youtube"][:6],
                    "tiktok_caption": f"{c['title']} {hs}"})
    return out


_BAD_WORDS = re.compile(r"\b(porra|caralho|merda|puta|foda|fdp|cacete|buceta|viado)\w*", re.IGNORECASE)


def clean_title(t: str) -> str:
    """Título sem aspas e sem palavrão: corta antes do palavrão; se sobrar pouco, devolve vazio."""
    t = " ".join(t.replace('"', "").split())
    t = re.sub(r"\s*\b(k{3,}|(ha){3,}|(rs){3,})\b", "", t, flags=re.IGNORECASE).strip()  # risada não é título
    m = _BAD_WORDS.search(t)
    if m:
        before = t[:m.start()]
        for sep in (".", "!", "?", ","):
            if sep in before:
                before = before[:before.rfind(sep)]
                break
        t = before.strip(" ,.-")
        if len(t.split()) < 3:
            return ""
    if len(t) > 70:
        t = t[:70].rsplit(" ", 1)[0] + "…"
    return t


def with_name(title: str, who: str) -> str:
    """Troca o genérico "Streamer"/"O streamer" pelo nome de verdade (chama mais atenção e ajuda na busca)."""
    if not who:
        return title
    name = who[:1].upper() + who[1:]
    words = title.split()
    for i, w in enumerate(words):
        if w.lower().strip(",.!?:;") == "streamer":
            if i > 0 and words[i - 1].lower() in ("o", "a"):
                words[i - 1] = ""
            words[i] = name + w[len("streamer"):]
            break
    out = " ".join(x for x in words if x)
    return out[:1].upper() + out[1:]


def _tag(word: str) -> str:
    """Hashtag limpa: uma palavra simples, minúscula. Palavras coladas (VaiPraPiscina) são descartadas."""
    raw = word.strip().lstrip("#")
    if sum(ch.isupper() for ch in raw[1:]) >= 2 or " " in raw.strip():
        return ""
    w = re.sub(r"[^\w]", "", raw.lower())
    return w if 2 < len(w) <= 16 and not w.isdigit() else ""


def build_hashtags(pool: dict, extra: list[str], streamer: str = "", limit: int = 6) -> list[str]:
    """Fixas do nicho + 1-2 do assunto do corte + nome do streamer + em alta, sem repetir."""
    order = pool["fixas"][:3] + [_tag(x) for x in extra[:2]] + [_tag(streamer)] +         pool["manuais_tiktok"][:2] + pool["em_alta_youtube"][:2] + pool["fixas"][3:]
    out = []
    for t in order:
        t = _tag(t)
        if t and t not in out:
            out.append(t)
    return out[:limit]


def build_posts(job: dict, clips: list[dict], transcript: dict, credit: bool = True,
                retitle: bool = False) -> list[dict]:
    """Metadados por corte, com a melhor IA disponível (Claude Haiku ou local); sem IA, um modelo simples."""
    pool = hashtag_pool()
    NL = chr(10)
    header = NL.join([
        f"Streamer: {job.get('streamer') or job.get('channel') or 'desconhecido'}",
        f"Live/vídeo: {job.get('title', '')}",
        "",
        f"Hashtags fixas do nicho: {', '.join(pool['fixas'])}",
        f"Em alta no TikTok (lista do usuário): {', '.join(pool['manuais_tiktok']) or 'nenhuma'}",
        f"Em alta nos Shorts das últimas 72h: {', '.join(pool['em_alta_youtube']) or 'sem dados'}",
        f"Termos buscados (tags de Shorts populares): {', '.join(pool['palavras_chave_youtube']) or 'sem dados'}",
        "",
    ])

    def block(i: int, c: dict) -> str:
        from .moments import words_between
        fala = words_between(transcript, c["start"], c["end"])[:1500]  # só o que é falado dentro do corte
        return NL.join([f'<corte index="{i}">', f"Título atual: {c['title']}", f"Gancho: {c.get('hook', '')}",
                        f"Fala: {fala}", "</corte>"])

    def claude():
        prompt = header + (NL + NL).join(block(i, c) for i, c in enumerate(clips))
        from .moments import TITLE_STYLE, with_learned
        system = with_learned(SYSTEM + "\n\nEstilo dos títulos:\n" + TITLE_STYLE)
        return [p.model_dump() for p in llm.claude_parse(system, prompt, PostPlan, effort="low").posts]

    def local():  # um corte por vez, só texto; hashtags vêm das listas (modelos pequenos inventam/repetem)
        out = []
        who = job.get("channel") or job.get("streamer") or ""  # nome de exibição ("TheDarkness")
        for i, c in enumerate(clips):
            # O título do corte já foi escrito e conferido com a fala do trecho final (moments.py).
            # A descrição NÃO é escrita pelo modelo pequeno (ele confunde fala do jogo com a do streamer e
            # inventa): é montada só com o que é certo: título, a frase real do corte e de quem é a live.
            from .moments import checked_title, quoted_hook, strip_name_prefix, title_for, words_between
            fala = words_between(transcript, c["start"], c["end"])[:1500]
            title, hook = c["title"], c.get("hook", "")
            if retitle:  # "Refazer títulos": escreve de novo a partir da fala + contexto
                from .config import JOBS_DIR
                from .moments import clip_context, scene_description
                before, viewers = clip_context(transcript, c, job.get("audience_clips"))
                src = JOBS_DIR / job["id"] / "source.mp4"
                scene = c.get("scene") or (scene_description(src, c["start"], c["end"]) if src.exists() else "")
                alts: list[str] = []
                try:
                    t = title_for(fala, job.get("title", ""), who, before=before, viewer_titles=viewers, scene=scene)
                    title, hook, alts = t.titulo, t.gancho or hook, t.alternativas
                except Exception as exc:
                    print(f"[seo] novo título falhou: {exc}")
                title, hook = checked_title(title, hook, fala, job.get("title", ""), who,
                                            before=before, viewer_titles=viewers, scene=scene, alternatives=alts,
                                            keep=c.get("title", ""))
            title = strip_name_prefix(with_name(clean_title(title) or title, who), who)
            tags = build_hashtags(pool, list(c.get("hashtags") or []), who)
            hs = " ".join("#" + t for t in tags)
            quote = quoted_hook(hook, fala)
            desc = NL.join(x for x in [title, quote, f"Corte da live de {who} jogando GTA 6." if who else ""] if x)
            out.append({"index": i, "youtube_title": title,
                        "youtube_description": desc + NL + NL + hs + " #shorts",
                        "youtube_tags": tags + [k for k in pool["palavras_chave_youtube"][:6] if k not in tags],
                        "tiktok_caption": f"{title} {hs}"})
        return out

    fb = _fallback(job, clips, pool)
    try:
        posts, provider = llm.run(claude, local)
        by_idx = {p["index"]: p for p in posts}
        posts = [by_idx.get(i, fb[i]) for i in range(len(clips))]
        print(f"[seo] títulos e hashtags por {provider}")
    except Exception as exc:
        print(f"[seo] usando modelo simples: {exc}")
        posts = fb

    cr = _credit(job) if credit else ""
    for p in posts:
        if cr and cr not in p["youtube_description"]:
            p["youtube_description"] = f"{p['youtube_description']}\n\n{cr}"
        p["youtube_title"] = p["youtube_title"][:100]
        p["tiktok_caption"] = p["tiktok_caption"][:2200]
    return posts
