"""Renderização dos cortes verticais (1080x1920) com legenda queimada."""
from __future__ import annotations

from pathlib import Path

from . import framing
from .config import FFMPEG
from .media import av_offset, run, video_encoder_args, video_size
from .subtitles import build_ass, hook_ass, strip_symbols

CPU_ENCODER = ("-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-profile:v", "high")


def run_encode(cmd: list, cwd) -> None:
    """Roda o ffmpeg com o codificador da placa (NVENC). Se a placa estiver ocupada (outra renderização ou a
    transcrição usando a GPU), tenta de novo pelo processador em vez de perder o corte ou o efeito."""
    try:
        run(cmd, cwd=cwd)
    except Exception as exc:
        if "h264_nvenc" not in cmd:
            raise
        i = cmd.index("-c:v")
        j = cmd.index("-profile:v", i) + 2  # os parâmetros da placa terminam no perfil
        print(f"[render] placa ocupada, codificando pelo processador ({str(exc).splitlines()[-1][:80]})")
        run(cmd[:i] + list(CPU_ENCODER) + cmd[j:], cwd=cwd)


LAYOUT_NAMES = {
    "split": "Webcam + jogo", "face": "Seguindo o rosto",
    "blur": "Fundo desfocado", "vertical": "Vertical original",
}


def render_clip(source: Path, transcript: dict, clip: dict, out_dir: Path, name: str,
                layout: str = "auto", title_overlay: bool = True, known_cam: list | None = None, locked: bool = False) -> dict:
    source, out_dir = Path(source).resolve(), Path(out_dir).resolve()  # o ffmpeg roda com cwd=out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    start, end = float(clip["start"]), float(clip["end"])
    src_w, src_h = video_size(source)

    info = framing.analyze(source, start, end, src_w, src_h, known_cam, locked) \
        if layout in ("auto", "split", "crop", "face") else {"kind": "none"}
    kind = framing.choose(layout, info, src_w, src_h)

    # No layout com webcam o topo está ocupado pela câmera; o título fica de fora.
    show_title = title_overlay and kind != "split"
    # No split, o título aparece como cartão de gancho nos primeiros 3 s, logo abaixo da webcam e da legenda
    card = (clip["title"], framing.top_height(info) + 240) \
        if title_overlay and kind == "split" and clip.get("title") else None
    hook = clip.get("cold_open")
    use_cold = bool(hook) and start + 3 <= float(hook[0]) < float(hook[1]) <= end
    ass_name = f"{name}.ass"
    (out_dir / ass_name).write_text(
        build_ass(transcript, start, end, clip.get("title") if show_title else None,
                  margin=framing.caption_margin(kind, info), shift=av_offset(source),
                  hook=None if use_cold else card),  # com clímax na frente, o cartão vai no vídeo final
        encoding="utf-8",
    )
    vf = framing.video_filter(kind, info, src_w, src_h) + f",setsar=1,ass={ass_name}[v]"
    mp4 = out_dir / f"{name}.mp4"
    cmd = [
        FFMPEG, "-y", "-v", "error",
        "-ss", f"{start:.3f}", "-i", str(source), "-t", f"{end - start:.3f}",
        "-filter_complex", vf,
        "-map", "[v]", "-map", "0:a:0?",
        *video_encoder_args(),
        "-pix_fmt", "yuv420p", "-r", "30",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        mp4.name,
    ]
    run_encode(cmd, out_dir)  # cwd = pasta do corte, assim o filtro ass não precisa escapar "C:\"
    if use_cold:
        add_cold_open(mp4, float(hook[0]) - start, float(hook[1]) - start, card)
    if clip.get("loop", True):
        add_loop(mp4)

    thumb = out_dir / f"{name}.jpg"
    mid = (end - start) / 2
    run([FFMPEG, "-y", "-v", "error", "-ss", f"{mid:.2f}", "-i", mp4.name,
         "-frames:v", "1", "-vf", "scale=360:-2", "-q:v", "4", thumb.name], cwd=out_dir)

    write_post_text(out_dir / f"{name}.txt", clip)
    out = {"file": mp4.name, "thumb": thumb.name, "layout_used": kind}
    if kind == "split" and info.get("cam") and (info.get("source") == "edges" or info.get("reused")):
        out["cam"] = [round(float(v), 1) for v in info["cam"]]  # estimativas não viram referência
    return out


def add_cold_open(mp4: Path, a: float, b: float, card: tuple[str, int] | None = None) -> None:
    """Gancho: abre o vídeo com o clímax (a..b, já com legenda) e depois volta ao começo da história,
    com um flash branco rápido na volta. Segura quem ia deslizar o vídeo no primeiro segundo.
    card: cartão de gancho (título) desenhado nos primeiros 3 s do vídeo final."""
    tmp = mp4.with_name(mp4.stem + "_gancho.mp4")
    card_ass = mp4.with_name(mp4.stem + "_cartao.ass")
    d = b - a
    tail = ";[cv]null[v]"
    if card:
        card_ass.write_text(hook_ass(*card), encoding="utf-8")
        tail = f";[cv]ass={card_ass.name}[v]"
    fc = (f"[0:v]split[v1][v2];[0:a]asplit[a1][a2];"
          f"[v1]trim={a:.3f}:{b:.3f},setpts=PTS-STARTPTS[hv];"
          f"[a1]atrim={a:.3f}:{b:.3f},asetpts=PTS-STARTPTS,afade=t=out:st={max(0.0, d - 0.08):.3f}:d=0.08[ha];"
          f"[v2]setpts=PTS-STARTPTS,fade=t=in:st=0:d=0.25:color=white[bv];"
          f"[a2]asetpts=PTS-STARTPTS,afade=t=in:st=0:d=0.05[ba];"
          f"[hv][ha][bv][ba]concat=n=2:v=1:a=1[cv][a]{tail}")
    try:
        run_encode([FFMPEG, "-y", "-v", "error", "-i", mp4.name, "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
             *video_encoder_args(), "-pix_fmt", "yuv420p", "-r", "30",
             "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", tmp.name], mp4.parent)
        tmp.replace(mp4)
    except Exception as exc:  # sem áudio ou erro do ffmpeg: fica o corte normal, sem gancho
        print(f"[render] gancho não aplicado: {exc}")
        tmp.unlink(missing_ok=True)
    finally:
        card_ass.unlink(missing_ok=True)


LOOP_SECONDS = 0.35


def add_loop(mp4: Path) -> None:
    """Final em loop: os últimos 0,35 s se fundem (imagem e som) no começo do vídeo. Quando o Short recomeça,
    a emenda fica natural e muita gente assiste de novo sem perceber (replay conta muito no algoritmo)."""
    from .media import duration
    tmp = mp4.with_name(mp4.stem + "_loop.mp4")
    try:
        d = duration(mp4)
        if d < 5:
            return
        x = LOOP_SECONDS
        fc = (f"[0:v]settb=AVTB,fps=30,split[v1][v2];[v2]trim=0:{x},setpts=PTS-STARTPTS[vh];"
              f"[v1][vh]xfade=transition=fade:duration={x}:offset={d - x:.3f}[v];"
              f"[0:a]asplit[a1][a2];[a2]atrim=0:{x},asetpts=PTS-STARTPTS[ah];"
              f"[a1][ah]acrossfade=d={x}[a]")
        run_encode([FFMPEG, "-y", "-v", "error", "-i", mp4.name, "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
             *video_encoder_args(), "-pix_fmt", "yuv420p", "-r", "30",
             "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", tmp.name], mp4.parent)
        tmp.replace(mp4)
    except Exception as exc:  # sem áudio ou erro do ffmpeg: fica o corte sem a emenda
        print(f"[render] loop não aplicado: {exc}")
        tmp.unlink(missing_ok=True)


def post_caption(clip: dict) -> str:
    tags = " ".join("#" + t.strip().lstrip("#").replace(" ", "") for t in clip.get("hashtags") or [] if t.strip())
    return f"{clip.get('title', '').strip()}\n\n{tags}".strip()  # emojis são bem-vindos no post


def write_post_text(path: Path, clip: dict) -> None:
    """Arquivo .txt ao lado do vídeo com o texto pronto para colar no TikTok/Shorts."""
    path.write_text(post_caption(clip) + "\n", encoding="utf-8")
