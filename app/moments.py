"""Cortes com começo, meio e fim para a IA local.

Modelos pequenos erram ao inventar tempos, mas avaliam e descrevem bem um trecho pronto. Então:
1. o sistema monta trechos candidatos a partir das frases (começa no início de uma fala, termina no fim de uma);
2. os sinais (volume, chat, exclamações, risadas) escolhem os candidatos mais promissores;
3. a IA local avalia cada trecho com o texto exato e o que foi dito antes: faz sentido sozinho? precisa do
   contexto anterior? qual a nota? e escreve um título só com o que acontece ali.
"""
from __future__ import annotations

import time

import numpy as np
from pydantic import BaseModel, Field

from . import llm

EXCITED = ("kkk", "haha", "hahaha", "rsrs", "meu deus", "nossa", "socorro", "que isso", "caralho", "porra",
           "não acredito", "mentira", "para", "olha isso", "ai meu", "mano do céu", "eita", "vixe", "corre")


class LocalJudge(BaseModel):
    # a ordem importa: o modelo pequeno primeiro entende o trecho, depois julga e dá o título
    o_que_acontece: str = Field(max_length=220, description="Uma frase: o que o STREAMER faz ou diz no trecho")
    quase_so_fala_do_jogo: bool = Field(description="O trecho é quase todo fala do jogo (personagens, rádio, propaganda)?")
    faz_sentido_sozinho: bool = Field(description="Quem nunca viu a live entende o que acontece no trecho?")
    precisa_contexto_anterior: bool = Field(description="O trecho depende do que foi dito antes para ser entendido?")
    nota: int = Field(ge=0, le=100, description="Potencial de viralizar como corte")
    titulo: str = Field(max_length=70, description="Até 9 palavras, só com fatos do TRECHO")
    gancho: str = Field(max_length=120)
    motivo: str = Field(max_length=160)
    hashtags: list[str] = Field(max_length=2)


# Estilo de título que rende nos Shorts (visto nos números do canal): a situação ou a curiosidade primeiro.
# Quem desliza procura o jogo e a situação, não o streamer.
TITLE_STYLE = """  Comece pela SITUAÇÃO ou pela CURIOSIDADE, nunca pelo nome do streamer. Não use "ele"/"ela" (você não sabe
  quem é o streamer): escreva a frase sem sujeito ("Capotou o carro…") ou com a situação como sujeito.
  Quando o assunto for o próprio GTA 6 (mapa, gráfico, detalhe escondido, comparação), cite "GTA 6" e
  prefira pergunta. Proibido título vago ("reagindo a alguém", "momento engraçado", "fala sobre algo").
  FORMATOS que funcionam (preencha com o que acontece NESTE trecho; nunca copie palavras daqui):
  "<descoberta> no GTA 6?", "Levou uma surra de <quem>", "<ação absurda> fugindo da polícia",
  "<fez algo> e se deu mal". Todo objeto, veículo, nome ou lugar do título tem que estar no trecho ou na tela."""

GAME_AUDIO = """ATENÇÃO: a transcrição mistura a fala do STREAMER (informal, gírias, "mano", "cara", reações) com o
ÁUDIO DO JOGO: diálogos de personagens e NPCs, rádio, propagandas, falas de missão (frases formais, traduzidas
ou em inglês). Nunca diga que o streamer falou ou fez o que é fala do jogo. Ex.: uma propaganda de rádio sobre
tinta de cabelo NÃO é o streamer dando tratamento capilar.
A transcrição é AUTOMÁTICA e tem erros: palavra que não existe ou não faz sentido ("lavasse", "coscastas")
foi mal transcrita. NUNCA use palavra estranha no título. Desconfie também de NÚMEROS e placares soltos
("5x0", "3 a 1"): muitas vezes são palavras mal reconhecidas (ex.: "Coca Zero" virou "5x0"). Só ponha número no
título se ele fizer sentido na frase e no contexto. Muito do que acontece é VISUAL (ação no jogo):
se a fala não explica o momento, use o contexto (o que foi dito antes, o título que os espectadores deram ao
clip) e descreva a situação/reação de forma natural, em português correto. O título tem que fazer sentido
para quem nunca viu a live."""

JUDGE_RULES = """Você é editor de cortes de lives para TikTok e YouTube Shorts. Avalie UM trecho.
Responda só com o JSON pedido.
""" + GAME_AUDIO + """
- o_que_acontece: uma frase simples sobre o que o streamer faz ou diz (e a situação do jogo, se ajudar).
- quase_so_fala_do_jogo: true se quase tudo é fala do jogo e o streamer quase não fala ou não reage.
- faz_sentido_sozinho: true se quem nunca viu a live entende o que acontece.
- precisa_contexto_anterior: true se o trecho depende do que foi dito antes para ser entendido.
- nota (0 a 100): alta para humor, reação forte, briga, susto, surpresa, história com final, opinião polêmica,
  curiosidade sobre o GTA 6. Baixa para conversa sem graça, leitura de chat/doações, explicação de menu,
  silêncio, frases soltas, trechos que são quase só fala do jogo e PAUSAS da live (ir ao banheiro, almoçar,
  pegar café, "já volto", AFK, ler/agradecer sub e doação, mexer em configuração ou menu).
- titulo: até 9 palavras dizendo EXATAMENTE o que acontece NO TRECHO DO CORTE (nunca no que foi dito antes),
  de acordo com o_que_acontece, citando o detalhe mais marcante (objeto, lugar ou ação concreta).
""" + TITLE_STYLE + """
  Sem palavrões, sem copiar a fala, sem inventar nomes ou fatos.
- gancho: a frase do STREAMER no trecho que mais prende atenção, copiada exatamente como está na fala.
- motivo: por que funciona (ou não) como corte.
- hashtags: até 2 palavras simples em minúsculas sobre o assunto."""


# ---------------------------------------------------------------- frases e candidatos

def sentence_units(transcript: dict) -> list[dict]:
    """Frases com tempo: quebra em pontuação final, pausa maior que 0,7 s ou 25 palavras."""
    words = [w for seg in transcript["segments"] for w in seg["words"]]
    units, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        gap = (nxt["s"] - w["e"]) if nxt else 99.0
        if w["w"].endswith((".", "!", "?", "…")) or gap > 0.7 or len(cur) >= 25:
            units.append({"s": cur[0]["s"], "e": cur[-1]["e"], "text": " ".join(x["w"] for x in cur),
                          "gap_after": gap})
            cur = []
    for i, u in enumerate(units):
        u["gap_before"] = units[i - 1]["gap_after"] if i else 99.0
    return units


TAILS = {"né", "sabe", "entendeu", "tá", "certo", "viu", "cara", "mano", "beleza", "ok", "então", "aí"}


def is_start(units: list[dict], i: int) -> bool:
    """Bom começo de corte: depois de uma frase terminada ou de uma pausa longa, e nunca num resto
    de frase como "né?" ou "sabe?"."""
    if i == 0:
        return True
    words = units[i]["text"].split()
    if len(words) <= 2 and words and words[0].lower().strip(" ?.!,…") in TAILS:
        return False
    if len(words) <= 3 and units[i]["text"][:1].islower():
        return False  # "force." solto em minúscula é o fim da frase anterior
    if units[i - 1]["text"].endswith((".", "!", "?", "…")):
        return True
    # depois de uma pausa longa só vale se for começo de frase (a transcrição começa frases com maiúscula);
    # "praia, né?" depois de uma pausa é o fim da frase anterior, não um começo
    return units[i]["gap_before"] >= 1.0 and units[i]["text"][:1].isupper()


def pick_end(units: list[dict], i: int, min_len: float, max_len: float) -> int | None:
    """Fim do corte começando na frase i: o fim de fala mais perto do alvo (meio da faixa),
    preferindo pausas e frases terminadas."""
    target = (min_len + max_len) / 2
    best, best_q = None, -1e9
    for j in range(i, len(units)):
        dur = units[j]["e"] - units[i]["s"]
        if dur > max_len:
            break
        if dur >= min_len:
            q = (min(units[j]["gap_after"], 2.0) + (0.6 if units[j]["text"].endswith((".", "!", "?")) else 0)
                 - 0.6 * abs(dur - target))
            if q > best_q:
                best, best_q = j, q
    return best


def candidates(units: list[dict], min_len: float, max_len: float) -> list[dict]:
    """Trechos que começam no início de uma fala e terminam no fim de uma, perto da duração alvo."""
    out = []
    min_words = max(18, int(0.7 * min_len))  # quase sem fala não dá para saber do que se trata
    for i, u in enumerate(units):
        if not is_start(units, i):
            continue  # começaria no meio de um raciocínio
        best = pick_end(units, i, min_len, max_len)
        if best is not None:
            text = " ".join(x["text"] for x in units[i:best + 1])
            if len(text.split()) < min_words:
                continue
            out.append({"i": i, "j": best, "s": u["s"], "e": units[best]["e"], "text": text})
    return out


class LocalTitle(BaseModel):
    o_que_acontece: str = Field(max_length=220, description="Uma frase: o que o STREAMER faz ou diz no trecho")
    titulo: str = Field(max_length=70, description="Até 9 palavras, só com fatos do TRECHO")
    gancho: str = Field(max_length=120)
    hashtags: list[str] = Field(max_length=2)


TITLE_RULES = """Você escreve títulos de cortes de lives para TikTok e YouTube Shorts. Responda só com o JSON pedido.
""" + GAME_AUDIO + """
- o_que_acontece: uma frase simples sobre o que o streamer faz ou diz (e a situação do jogo, se ajudar).
- titulo: até 9 palavras dizendo o que acontece no TRECHO, de acordo com o_que_acontece, com o detalhe mais
  marcante (objeto, lugar ou ação concreta). Baseie-se no trecho e no contexto dado; não invente fatos.
  Frase natural e com sentido; se a fala for confusa, prefira descrever a reação ("Ficou chocado com…",
  "Não acreditou no tamanho de…") a juntar palavras soltas da transcrição.
""" + TITLE_STYLE + """
  Sem palavrões, sem copiar a fala, sem inventar nomes ou fatos.
- gancho: a frase do STREAMER no trecho que mais prende atenção, copiada exatamente como está na fala.
- hashtags: até 2 palavras simples em minúsculas sobre o assunto."""


class GroqTitle(BaseModel):  # na Groq: 3 opções e a escolha da mais chamativa (mesma chamada)
    o_que_acontece: str = Field(max_length=220)
    opcoes: list[str] = Field(min_length=3, max_length=3)
    melhor: int = Field(ge=0, le=2)
    gancho: str = Field(max_length=120)
    hashtags: list[str] = Field(max_length=2)


class TitleResult(BaseModel):
    o_que_acontece: str = ""
    titulo: str
    gancho: str = ""
    hashtags: list[str] = []
    alternativas: list[str] = []


CLOUD_TITLE_EXTRA = """
- opcoes: 3 títulos BEM diferentes entre si, cada um seguindo as regras do titulo acima:
  1) curiosidade ou descoberta, 2) pergunta, 3) a reação ou o absurdo do momento.
- melhor: o índice (0, 1 ou 2) do título mais CHAMATIVO para o feed de Shorts, o que faria alguém parar de
  rolar para assistir, sem mentir sobre o que acontece no vídeo."""


def title_for(text: str, live_title: str, streamer: str, avoid: str = "", cloud: bool = True,
              before: str = "", viewer_titles: list[str] | None = None, scene: str = "") -> LocalTitle:
    """Título escrito a partir do texto FINAL do corte (depois de qualquer ajuste de início/fim).
    before: o que foi dito logo antes (contexto); viewer_titles: títulos que espectadores deram ao clip
    desse momento na Twitch; scene: descrição das imagens do corte (o que se VÊ, que a transcrição não pega)."""
    lines = [f"Live: {live_title or 'desconhecida'}" + (f" — streamer {streamer}" if streamer else "")]
    if scene:
        lines += ["", "O QUE APARECE NA TELA (descrição automática das imagens do corte):", scene]
    if viewer_titles:
        lines += ["", "TÍTULOS QUE ESPECTADORES DERAM AO CLIP DESTE MOMENTO (contexto do que aparece na tela):",
                  *[f'- "{t}"' for t in viewer_titles[:4]]]
    if before:
        lines += ["", "DITO LOGO ANTES (contexto, não está no corte):", f'"{before}"']
    lines += ["", "TRECHO DO CORTE (transcrição automática, pode ter erros):", f'"{text}"']
    if avoid:
        lines.append(f'\nO título "{avoid}" NÃO serve (não combina ou não faz sentido). Escreva outro.')
    prompt = "\n".join(lines)
    if cloud and llm.groq_key():
        # Groq: 3 opções e escolhe a mais chamativa; as outras ficam de reserva (se a escolhida não passar
        # na checagem contra a fala, tenta a próxima antes de pedir de novo)
        try:
            wait = llm._groq_paused_until - time.time()
            if 0 < wait <= 90:
                time.sleep(wait + 1)
            if llm.groq_ready():
                g = llm.groq_parse(with_learned(TITLE_RULES + CLOUD_TITLE_EXTRA), prompt, GroqTitle)
                opts = [o.strip() for o in g.opcoes if o.strip()]
                best = opts[g.melhor] if g.melhor < len(opts) else opts[0]
                return TitleResult(o_que_acontece=g.o_que_acontece, titulo=best, gancho=g.gancho,
                                   hashtags=g.hashtags, alternativas=[o for o in opts if o != best])
        except Exception as exc:
            print(f"[ia] Groq (3 títulos): {exc}")
    t = llm.title_parse(with_learned(TITLE_RULES), prompt, LocalTitle, cloud=cloud)
    return TitleResult(**t.model_dump())


# ---------------------------------------------------------------- título que bate com a fala
STOP = set("""a o as os um uma uns umas de da do das dos em no na nos nas por pra para com sem que se e ou mas
mais muito pouco ele ela eles elas isso esse essa este esta aqui ali la lá vai vou foi tem ter tá ta to tô
ser sua seu suas seus meu minha nosso nossa como quando onde quem porque sobre até após entre já nao não
sim gta vi 6 v streamer live corte momento fala falando diz dizendo reage reação faz fazendo""".split())


def _fold(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFD", s.lower())
    return "".join(ch for ch in s if ch.isalnum() and unicodedata.category(ch) != "Mn")


def grounded(title: str, text: str, streamer: str = "", extra: str = "") -> bool:
    """O título usa pelo menos uma palavra de conteúdo que aparece na fala ou no contexto (raiz de 5 letras)?
    Pega título totalmente inventado sem exigir cópia da fala. extra: contexto (fala anterior, título dos
    espectadores, resumo do que acontece)."""
    name = _fold(streamer)
    spoken = {_fold(w)[:5] for w in (text + " " + extra).split() if len(_fold(w)) >= 4}
    keys = [_fold(w) for w in title.split()]
    keys = [k for k in keys if len(k) >= 4 and k not in STOP and k != name]
    return any(k[:5] in spoken for k in keys)


def quoted_hook(hook: str, text: str) -> str:
    """Plano B de título: a frase marcante do próprio trecho, entre aspas (sempre combina com o vídeo)."""
    hook = " ".join((hook or "").split()).strip(" .,")
    if not hook:
        return ""
    words = [_fold(w) for w in hook.split() if _fold(w)]
    spoken = {_fold(w) for w in text.split()}
    if not words or sum(w in spoken for w in words) / len(words) < 0.7:
        return ""  # o "gancho" não está na fala
    from .seo import _BAD_WORDS
    if _BAD_WORDS.search(hook) or len(hook) > 60 or len(hook.split()) < 3:
        return ""  # com palavrão a frase ficaria cortada pela metade; curta demais não diz nada
    h = hook
    return f"“{h[:1].upper() + h[1:]}”"


def strip_name_prefix(title: str, streamer: str) -> str:
    """'Alanzoka: tirando o rastreador' -> 'Tirando o rastreador'; 'Alanzoka nega uso de bong' -> 'Nega uso de
    bong'. Nos números do canal, título começando pelo nome do streamer rendeu bem menos."""
    t = title.strip()
    if not streamer:
        return t
    for sep in (":", " -", " –", ",", " "):
        if t.lower().startswith(streamer.lower() + sep):
            rest = t[len(streamer) + len(sep):].strip(" -–:,")
            if len(rest.split()) >= 2:
                return rest[:1].upper() + rest[1:]
    return t


def checked_title(title: str, hook: str, text: str, live_title: str, streamer: str,
                  cloud: bool = True, before: str = "", viewer_titles: list[str] | None = None,
                  scene: str = "", alternatives: list[str] | None = None, keep: str = "") -> tuple[str, str]:
    """Título que combina com o trecho: confere; se não combinar, tenta as outras opções, depois pede outro;
    por fim usa a fala entre aspas. Retorna (título, gancho)."""
    extra = " ".join([before, scene, *(viewer_titles or [])])
    for cand in [title, *(alternatives or [])]:
        cand = strip_name_prefix(cand, streamer)
        if cand and grounded(cand, text, streamer, extra):
            return cand, hook
    title = strip_name_prefix(title, streamer)
    try:
        t = title_for(text, live_title, streamer, avoid=title, cloud=cloud, before=before,
                      viewer_titles=viewer_titles, scene=scene)
        for new in [t.titulo, *getattr(t, "alternativas", [])]:
            new = strip_name_prefix(new, streamer)
            if new and grounded(new, text, streamer, extra):
                return new, t.gancho or hook
        hook = t.gancho or hook
    except Exception as exc:
        print(f"[ia] novo título falhou: {exc}")
    # nada passou na checagem: a fala marcante entre aspas; senão o título anterior (se ele bater com a fala);
    # um título reprovado só em último caso
    kept = strip_name_prefix(keep, streamer) if keep else ""
    if kept and not grounded(kept, text, streamer, extra):
        kept = ""
    return quoted_hook(hook, text) or kept or title, hook


def with_learned(rules: str) -> str:
    """Acrescenta a dica de título aprendida com as views do canal (quando já há números suficientes)."""
    from .learn import title_hint
    hint = title_hint()
    return rules + ("\n" + hint if hint else "")


def _signal(c: dict, curve: np.ndarray | None) -> float:
    txt = c["text"].lower()
    text_sig = sum(txt.count(k) for k in EXCITED) + 0.3 * (txt.count("!") + txt.count("?"))
    dur = max(c["e"] - c["s"], 1.0)
    density = len(txt.split()) / dur  # fala corrida costuma ser momento quente
    sig = 0.0
    if curve is not None and len(curve):
        a, b = int(c["s"]), min(int(c["e"]) + 1, len(curve))
        if b > a:
            vals = np.sort(curve[a:b])
            sig = float(np.mean(vals[-max(3, len(vals) // 4):]))
    from .highlights import DISCOVERY  # curiosidade do GTA 6: garante que esses trechos sejam avaliados
    discovery = len(set(m.group(0).lower() for m in DISCOVERY.finditer(txt)))
    return sig + 0.35 * text_sig + 0.15 * density + 0.6 * min(discovery, 3)


def preselect(cands: list[dict], curve, k: int) -> list[dict]:
    """Os k candidatos mais promissores pelos sinais, sem sobreposição."""
    for c in cands:
        c["signal"] = _signal(c, curve)
    chosen = []
    for c in sorted(cands, key=lambda x: -x["signal"]):
        if all(min(c["e"], o["e"]) - max(c["s"], o["s"]) < 3 for o in chosen):
            chosen.append(c)
        if len(chosen) >= k:
            break
    return chosen


# ---------------------------------------------------------------- avaliação pela IA local

def local_clips(transcript: dict, curve, settings: dict, live_title: str, streamer: str = "") -> list[dict]:
    units = sentence_units(transcript)
    min_len, max_len = float(settings["min_len"]), float(settings["max_len"])
    pool = preselect(candidates(units, min_len, max_len), curve, max(6, int(settings["num_clips"]) * 3))
    if not pool:
        return []
    sigs = sorted(c["signal"] for c in pool)
    ideas = []
    for c in pool:
        before = " ".join(u["text"] for u in units[max(0, c["i"] - 2):c["i"]])
        prompt = "\n".join([
            f"Live: {live_title or 'desconhecida'}" + (f" — streamer {streamer}" if streamer else ""),
            "",
            "DITO ANTES DO TRECHO (contexto, não entra no corte):",
            f'"{before or "(início da live)"}"',
            "",
            f"TRECHO DO CORTE ({c['e'] - c['s']:.0f} segundos):",
            f'"{c["text"]}"',
        ])
        try:
            j = llm.ollama_parse(with_learned(JUDGE_RULES), prompt, LocalJudge, num_ctx=4096)
        except Exception as exc:
            print(f"[ia] avaliação de um trecho falhou: {exc}")
            continue
        s, e = c["s"], c["e"]
        title, hook, tags = j.titulo, j.gancho, j.hashtags
        summary = j.o_que_acontece
        final_text = c["text"]
        if j.precisa_contexto_anterior:
            # começa antes, num início de frase de verdade, e reposiciona o fim para manter a duração
            # (o contexto entra DENTRO do tempo do corte)
            for k in range(c["i"] - 1, max(-1, c["i"] - 4), -1):
                if not is_start(units, k):
                    continue
                end_j = pick_end(units, k, min_len, max_len)
                if end_j is None or end_j < c["i"]:  # o momento principal tem que continuar dentro
                    continue
                if len(" ".join(u["text"] for u in units[k:end_j + 1]).split()) < max(18, int(0.7 * min_len)):
                    continue  # o trecho deslocado ficaria quase sem fala
                s, e = units[k]["s"], units[end_j]["e"]  # o momento principal continua dentro
                break
            if s < c["s"]:
                # o corte mudou de lugar: o título tem que falar do que ficou DENTRO dele
                final_text = " ".join(u["text"] for u in units[k:end_j + 1])
                try:
                    t = title_for(final_text, live_title, streamer, cloud=False)
                    title, hook, tags, summary = t.titulo, t.gancho, t.hashtags, t.o_que_acontece
                except Exception as exc:
                    print(f"[ia] título do trecho ajustado falhou: {exc}")
        # confere se o título combina com a fala; se não, pede outro ou usa a frase marcante entre aspas
        title, hook = checked_title(title, hook, final_text, live_title, streamer, cloud=False)
        nota = j.nota * (1.0 if j.faz_sentido_sozinho else 0.6)
        if j.quase_so_fala_do_jogo:
            nota *= 0.5  # propaganda de rádio, diálogo de missão: o streamer quase não aparece
        sig_pct = 100.0 * sigs.index(c["signal"]) / max(len(sigs) - 1, 1)
        # o modelo local dá notas modestas (centradas em ~50): reescala para o melhor do bloco ficar
        # alto e os fracos continuarem baixos, compatível com a "nota mínima" da Agenda
        score = int(round(min(99, 10 + 0.9 * nota + 0.3 * sig_pct)))
        ideas.append({"start": s, "end": e, "title": title, "hook": hook, "reason": j.motivo,
                      "score": score, "hashtags": tags, "summary": summary,
                      "context_added": s < c["s"]})
    ideas.sort(key=lambda x: -x["score"])
    return ideas


SCENE_PROMPT = """São 3 quadros (início, meio e fim) de um trecho de live de GTA 6, lado a lado.
A webcam do streamer pode aparecer num canto. Em 1 ou 2 frases, em português, descreva o que acontece NO JOGO:
ações, lugar, veículos, personagens, objetos marcantes, se algo mudou do início ao fim.
Ignore a webcam e qualquer texto ou interface sobreposta. Seja concreto e não invente."""


def scene_description(source, start: float, end: float) -> str:
    """O que aparece na tela no corte (3 quadros do vídeo ORIGINAL, sem legenda), descrito pela Groq.
    É o que a transcrição não pega: boa parte dos momentos de jogo é visual."""
    import cv2
    try:
        cap = cv2.VideoCapture(str(source))
        frames = []
        for f in (0.2, 0.5, 0.85):
            cap.set(cv2.CAP_PROP_POS_MSEC, (start + f * (end - start)) * 1000)
            ok, img = cap.read()
            if ok:
                h, w = img.shape[:2]
                frames.append(cv2.resize(img, (480, int(h * 480 / w))))
        cap.release()
        if not frames:
            return ""
        ok, buf = cv2.imencode(".jpg", cv2.hconcat(frames), [cv2.IMWRITE_JPEG_QUALITY, 70])
        return llm.groq_vision(SCENE_PROMPT, buf.tobytes()) if ok else ""
    except Exception as exc:
        print(f"[ia] descrição da cena falhou: {exc}")
        return ""


def clip_context(transcript: dict, c: dict, audience_clips: list[dict] | None) -> tuple[str, list[str]]:
    """Contexto para o título: o que foi dito nos ~25 s antes do corte e os títulos dos clips que os
    espectadores fizeram desse momento na Twitch (descrevem o que aparece na tela)."""
    before = words_between(transcript, max(0.0, c["start"] - 25), c["start"])[-400:]
    viewers = [a["title"].strip() for a in (audience_clips or [])
               if c["start"] - 10 <= a.get("t", -1e9) <= c["end"] + 10 and (a.get("title") or "").strip()]
    return before, list(dict.fromkeys(viewers))


class RerankItem(BaseModel):
    # régua do gancho (soma 0 a 10): critérios explícitos deixam a nota consistente e explicável
    id: int
    gancho: int = Field(ge=0, le=3, description="Os primeiros segundos prendem ou abrem uma curiosidade?")
    momento_forte: int = Field(ge=0, le=2, description="Humor, reação genuína, susto, fail, briga, descoberta do GTA 6")
    desfecho_no_corte: int = Field(ge=0, le=2, description="O clímax/desfecho acontece DENTRO do corte?")
    entende_sozinho: int = Field(ge=0, le=1, description="Quem nunca viu a live entende?")
    entra_direto: int = Field(ge=0, le=1, description="Começa sem enrolação?")
    detalhe_especifico: int = Field(ge=0, le=1, description="Tem um detalhe concreto: nome, objeto, número, lugar?")
    motivo: str = Field(max_length=160)


class Rerank(BaseModel):
    itens: list[RerankItem]


RUBRIC = (("gancho", "Gancho", 3), ("momento_forte", "Momento", 2), ("desfecho_no_corte", "Desfecho", 2),
          ("entende_sozinho", "Sozinho", 1), ("entra_direto", "Direto", 1), ("detalhe_especifico", "Detalhe", 1))


def rubric_score(it) -> int:
    """Soma da régua (0 a 10) em escala 0 a 100."""
    return 10 * sum(min(max(int(getattr(it, k)), 0), mx) for k, _, mx in RUBRIC)


def rubric_text(it) -> str:
    return " · ".join(f"{label} {getattr(it, k)}/{mx}" for k, label, mx in RUBRIC)


RERANK_RULES = """Você é um editor experiente de cortes de lives de GTA 6 para YouTube Shorts no Brasil.
Recebe os FINALISTAS de um bloco da live e avalia cada um, como Short de ~30 segundos para quem NUNCA viu o
streamer, numa RÉGUA de critérios. Responda só com o JSON pedido, um item por finalista (id igual).
""" + GAME_AUDIO + """
RÉGUA (seja exigente; a soma típica fica perto de 5; 8+ só para momento realmente forte):
- gancho (0-3): os primeiros segundos prendem? Abre uma curiosidade ("o que vai acontecer?", "como assim?")?
- momento_forte (0-2): humor que funciona sozinho, reação forte e genuína, susto, briga, fail épico, momento
  absurdo do jogo, curiosidade/descoberta do GTA 6 (mapa, gráfico, detalhe escondido, comparação com o GTA V).
- desfecho_no_corte (0-2): o clímax, a piada ou a resposta acontece DENTRO do trecho? (0 se corta antes)
- entende_sozinho (0-1): dá para entender sem ter visto a live?
- entra_direto (0-1): começa no assunto, sem enrolação?
- detalhe_especifico (0-1): tem algo concreto (nome, objeto, número, lugar) para o título?
Zero em tudo para: conversa comum, explicação de menu ou missão, leitura de chat/doação, pausas (banheiro,
comida, "já volto"), ou trecho que é só áudio do jogo sem reação do streamer.
motivo: uma frase curta do porquê."""


def groq_rerank(ideas: list[dict], transcript: dict, live_title: str, streamer: str,
                audience_clips: list[dict] | None = None, source=None, k: int = 6) -> int:
    """A IA local separa os candidatos; a Groq (que entende muito melhor o que é engraçado ou chocante, e vê as
    imagens do corte) reavalia os k melhores. Nota final = 65% Groq + 35% local. Retorna quantos reavaliou."""
    if not llm.groq_ready() or not ideas:
        return 0
    finalists = sorted(ideas, key=lambda d: -int(d.get("score", 0)))[:k]
    blocks = []
    for i, d in enumerate(finalists):
        text = words_between(transcript, d["start"], d["end"])
        before, viewers = clip_context(transcript, d, audience_clips)
        if source and not d.get("scene"):
            d["scene"] = scene_description(source, d["start"], d["end"])
        blocks.append("\n".join(x for x in [
            f"<finalista id=\"{i}\">",
            f"Na tela: {d['scene']}" if d.get("scene") else "",
            f"Títulos que espectadores deram ao clip: {', '.join(viewers)}" if viewers else "",
            f"Dito antes: \"{before[-200:]}\"" if before else "",
            f"Fala do corte: \"{text[:900]}\"",
            "</finalista>"] if x))
    prompt = (f"Live: {live_title or 'desconhecida'}" + (f" — streamer {streamer}" if streamer else "")
              + "\n\n" + "\n\n".join(blocks))
    try:
        res = llm.groq_parse(RERANK_RULES, prompt, Rerank)
    except Exception as exc:
        print(f"[ia] reavaliação pela Groq falhou: {exc}")
        return 0
    got = {it.id: it for it in res.itens if 0 <= it.id < len(finalists)}
    for i, d in enumerate(finalists):
        it = got.get(i)
        if not it:
            continue
        g = rubric_score(it)
        d["groq_score"] = g
        d["rubric"] = {k: int(getattr(it, k)) for k, _, _ in RUBRIC}
        d["score"] = int(round(0.65 * g + 0.35 * int(d.get("score", 0))))
        d["reason"] = f"Groq {g} ({rubric_text(it)}): {it.motivo} " + (d.get("reason") or "")
    if got:  # quem não foi finalista não pode passar na frente de um finalista reavaliado
        floor = min(d["score"] for d in finalists if "groq_score" in d)
        for d in ideas:
            if d not in finalists:
                d["score"] = min(int(d.get("score", 0)), floor - 1)
    ideas.sort(key=lambda d: -int(d.get("score", 0)))
    return len(got)


def refine_titles(clips: list[dict], transcript: dict, live_title: str, streamer: str,
                  audience_clips: list[dict] | None = None, source=None) -> int:
    """Título final dos cortes escolhidos escrito pela Groq (grátis, bem melhor que o modelo local pequeno),
    a partir da fala do corte + contexto + o que aparece na tela. Poucas chamadas por bloco."""
    if not llm.groq_ready():
        return 0
    from .seo import _tag, clean_title
    done = 0
    for c in clips:
        text = words_between(transcript, c["start"], c["end"])
        before, viewers = clip_context(transcript, c, audience_clips)
        scene = c.get("scene") or (scene_description(source, c["start"], c["end"]) if source else "")
        if scene:
            c["scene"] = scene
        try:
            t = title_for(text, live_title, streamer, cloud=True, before=before, viewer_titles=viewers, scene=scene)
        except Exception as exc:
            print(f"[ia] título pela Groq falhou: {exc}")
            continue
        title, hook = checked_title(t.titulo, t.gancho or c.get("hook", ""), text, live_title, streamer, cloud=True,
                                    before=before, viewer_titles=viewers, scene=scene, alternatives=t.alternativas,
                                    keep=c.get("title", ""))
        title = strip_name_prefix(clean_title(title) or title, streamer)
        if not title:
            continue
        c.update(title=title[:1].upper() + title[1:], hook=hook, summary=t.o_que_acontece, title_by="Groq",
                 hashtags=[x for x in (_tag(h) for h in t.hashtags) if x][:2] or c.get("hashtags", []))
        done += 1
        if not llm.groq_ready():  # bateu no limite no meio: os outros ficam com o título local
            break
    return done


def words_between(transcript: dict, start: float, end: float) -> str:
    """Só as palavras faladas DENTRO do corte (para títulos que combinam com o que se vê)."""
    return " ".join(w["w"] for seg in transcript["segments"] for w in seg["words"]
                    if w["e"] > start and w["s"] < end)
