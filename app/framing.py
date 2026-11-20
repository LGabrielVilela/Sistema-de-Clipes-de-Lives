"""Enquadramento inteligente: detecta rosto/webcam e decide como montar o vídeo 9:16.

Layouts:
- split : webcam em cima + jogo embaixo (stream com câmera num canto)
- face  : tela cheia acompanhando o rosto (podcast, IRL, react com câmera grande)
- blur  : vídeo inteiro no meio com fundo desfocado (quando não há rosto)

A moldura da webcam é achada pelas bordas que ficam paradas enquanto o jogo se mexe.
"""
from __future__ import annotations

import subprocess
import threading
import urllib.request
from pathlib import Path

import numpy as np

from .config import DATA_DIR, FFMPEG
from .media import NO_WINDOW

SW, OUT_W, OUT_H = 960, 1080, 1920  # largura da análise e saída final
YUNET_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
             "face_detection_yunet_2023mar.onnx")
YUNET_PATH = DATA_DIR / "models" / "face_detection_yunet_2023mar.onnx"
_det_lock = threading.Lock()


def _even(v: float) -> int:
    return max(2, int(round(v / 2)) * 2)


def _sample(source: Path, start: float, end: float, sh: int, n: int = 24) -> list[np.ndarray]:
    dur = max(end - start, 1.0)
    fps = min(2.0, n / dur)
    cmd = [FFMPEG, "-v", "error", "-ss", f"{start:.2f}", "-t", f"{dur:.2f}", "-i", str(source),
           "-vf", f"fps={fps:.4f},scale={SW}:{sh}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    raw = subprocess.run(cmd, capture_output=True, creationflags=NO_WINDOW).stdout
    size = SW * sh * 3
    return [np.frombuffer(raw[i * size:(i + 1) * size], np.uint8).reshape(sh, SW, 3)
            for i in range(len(raw) // size)]


def _ensure_yunet() -> bool:
    if YUNET_PATH.exists() and YUNET_PATH.stat().st_size > 100_000:
        return True
    try:
        YUNET_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = YUNET_PATH.with_suffix(".tmp")
        urllib.request.urlretrieve(YUNET_URL, tmp)
        tmp.replace(YUNET_PATH)
        return True
    except Exception as exc:
        print(f"[rosto] não foi possível baixar o YuNet ({exc}); usando detector simples")
        return False


def _detect_faces(frames: list[np.ndarray]) -> list[list[tuple[float, float, float, float]]]:
    """Lista de rostos (x, y, w, h) por quadro, em coordenadas da análise (960 de largura)."""
    import cv2
    h, w = frames[0].shape[:2]
    out = []
    with _det_lock:
        if _ensure_yunet():
            det = cv2.FaceDetectorYN.create(str(YUNET_PATH), "", (w, h), 0.6)
            for f in frames:
                _, faces = det.detect(f)
                out.append([] if faces is None else [tuple(map(float, x[:4])) for x in faces])
            return out
        casc = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        for f in frames:
            g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
            out.append([tuple(map(float, r)) for r in casc.detectMultiScale(g, 1.1, 6, minSize=(12, 12))])
    return out


def _stable_faces(per_frame: list[list[tuple]], n_frames: int) -> list[list[tuple]]:
    """Mantém só rostos que ficam no mesmo lugar na maioria dos quadros (quem está na câmera).
    Personagens do jogo andam pela tela e são descartados. Também agrupa só rostos próximos
    entre si (duas pessoas na mesma webcam)."""
    allf = [f for fs in per_frame for f in fs]
    if not allf:
        return per_frame

    def center(f):
        return f[0] + f[2] / 2, f[1] + f[3] / 2

    def hits(f):
        cx, cy = center(f)
        tol = max(f[2] * 0.6, 0.02 * SW)
        return sum(any(abs(center(g)[0] - cx) < tol and abs(center(g)[1] - cy) < tol
                       and 0.6 < g[2] / max(f[2], 1) < 1.6 for g in fs) for fs in per_frame)

    counts = {id(f): hits(f) for f in allf}
    stable = [f for f in allf if counts[id(f)] >= max(2, 0.4 * n_frames)]
    if not stable:
        return [[] for _ in per_frame]
    anchor = max(stable, key=lambda f: (counts[id(f)], f[2]))  # o rosto mais constante
    ax, ay = center(anchor)
    near = lambda f: abs(center(f)[0] - ax) < 0.22 * SW and abs(center(f)[1] - ay) < 0.25 * SW  # noqa: E731
    # vizinhos do âncora (2ª pessoa na mesma câmera, às vezes de perfil) precisam de menos aparições
    keep = [f for f in allf if near(f) and counts[id(f)] >= max(2, 0.15 * n_frames)]
    return [[f for f in fs if any(f is k or (abs(center(f)[0] - center(k)[0]) < k[2] * 0.6
                                            and abs(center(f)[1] - center(k)[1]) < k[3] * 0.6) for k in keep)]
            for fs in per_frame]


def _find_box(frames, fx0, fy0, fx1, fy1):
    """Procura a moldura da webcam em volta dos rostos usando bordas persistentes."""
    import cv2
    grays = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for f in frames]
    gx = np.mean([np.abs(cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)) for g in grays], axis=0)
    gy = np.mean([np.abs(cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)) for g in grays], axis=0)
    H, W = grays[0].shape
    pad = max(8, (fy1 - fy0))
    rows = slice(int(max(fy0 - pad, 0)), int(min(fy1 + pad, H)))
    cols = slice(int(max(fx0 - pad, 0)), int(min(fx1 + pad, W)))
    colp, rowp = gx[rows].mean(axis=0), gy[:, cols].mean(axis=1)
    cmed, rmed = np.median(colp) + 1e-6, np.median(rowp) + 1e-6

    def peak(prof, med, start, stop, edge):
        """Primeira borda forte saindo do rosto em direção a `stop` (a moldura é a mais próxima)."""
        start, stop = int(start), int(stop)
        step = 1 if stop > start else -1
        start = max(0, min(start, len(prof) - 1))
        stop = max(-1, min(stop, len(prof)))
        best_i, best_r = edge, 0.0
        for i in range(start, stop, step):
            r = prof[i] / med
            if r > best_r:
                best_i, best_r = i, r
            # achou uma borda forte e já passou do pico dela: é a moldura
            if best_r > 4.0 and r < best_r * 0.5:
                return best_i, float(best_r)
        return best_i, float(best_r)

    # cada lado pode ser a borda da tela (webcam encostada no canto)
    left, sl = peak(colp, cmed, fx0 - 2, fx0 - 0.45 * W, 0)
    right, sr = peak(colp, cmed, fx1 + 2, fx1 + 0.45 * W, W)
    top, st = peak(rowp, rmed, fy0 - 2, fy0 - 0.5 * H, 0)
    bottom, sb = peak(rowp, rmed, fy1 + 2, fy1 + 0.5 * H, H)
    ok = lambda s: s > 4.0  # noqa: E731
    left = left if ok(sl) else (0 if fx0 < 0.25 * W else None)
    right = right if ok(sr) else (W if fx1 > 0.75 * W else None)
    top = top if ok(st) else (0 if fy0 < 0.25 * H else None)
    bottom = bottom if ok(sb) else (H if fy1 > 0.75 * H else None)
    if None in (left, right, top, bottom):
        return None
    bw, bh = right - left, bottom - top
    if not (0.1 * W <= bw <= 0.55 * W and 0.1 * H <= bh <= 0.65 * H):
        return None
    if not (1.1 <= bw / max(bh, 1) <= 2.3):  # webcams ficam entre 4:3 e 16:9 (com folga)
        return None
    return left, top, right, bottom


def _box_still_there(frames, box_px: list[float], k: float) -> bool:
    """Confere se as bordas de uma moldura de webcam já conhecida continuam no mesmo lugar."""
    import cv2
    grays = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for f in frames]
    gx = np.mean([np.abs(cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)) for g in grays], axis=0)
    gy = np.mean([np.abs(cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)) for g in grays], axis=0)
    H, W = grays[0].shape
    l, t, r, b = (int(round(v / k)) for v in box_px)
    l, r, t, b = max(0, min(l, W - 1)), max(0, min(r, W)), max(0, min(t, H - 1)), max(0, min(b, H))
    if r - l < 20 or b - t < 20:
        return False
    if (r - l) * (b - t) > 0.3 * W * H:  # webcam nunca ocupa um terço da tela
        return False
    cmed, rmed = np.median(gx) + 1e-6, np.median(gy) + 1e-6
    interior, strong = 0, 0
    for x, at_edge in ((l, l <= 2), (r - 1, r >= W - 2)):
        if not at_edge:
            interior += 1
            strong += gx[t:b, max(0, x - 2):x + 3].max(axis=1).mean() / cmed > 3.0
    for y, at_edge in ((t, t <= 2), (b - 1, b >= H - 2)):
        if not at_edge:
            interior += 1
            strong += gy[max(0, y - 2):y + 3, l:r].max(axis=0).mean() / rmed > 3.0
    # a beira da tela não prova nada: as bordas internas (pelo menos 2) precisam estar lá, TODAS.
    # (O streamer pode mudar a webcam de lugar no meio da live; moldura velha aceita à toa corta a cabeça.)
    return interior >= 2 and strong == interior


def _snap_sides(frames, box) -> tuple:
    """Encosta cada lado da moldura numa borda real do painel da webcam. Um lado sem linha forte (ex.: a caixa
    foi até a beira da tela) procura para dentro a linha que atravessa quase toda a altura/largura do painel,
    e só encolhe se a faixa cortada for jogo (se mexe ou é escura); faixa do quarto (parada e iluminada) fica.
    Tudo na escala da análise."""
    import cv2
    grays = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for f in frames]
    stack = np.stack(grays).astype(np.float32)
    gx = np.mean([np.abs(cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)) for g in grays], axis=0)
    gy = np.mean([np.abs(cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)) for g in grays], axis=0)
    H, W = grays[0].shape
    cmed, rmed = np.median(gx) + 1e-6, np.median(gy) + 1e-6
    l, t, r, b = (int(round(v)) for v in box)
    l, r, t, b = max(0, l), min(W, r), max(0, t), min(H, b)
    if r - l < 30 or b - t < 30:
        return box

    def vline(x):  # fração das linhas do painel com borda vertical forte em x
        return float((gx[t:b, max(0, x - 1):x + 2].max(axis=1) / cmed > 3.0).mean())

    def hline(y):
        return float((gy[max(0, y - 1):y + 2, l:r].max(axis=0) / rmed > 3.0).mean())

    def is_game(x0, x1, y0, y1):  # a faixa que sairia é jogo? (se mexe ou fica escura)
        s = stack[:, y0:y1, x0:x1]
        if s.size == 0:
            return False
        moving = (np.std(s, axis=0) > 6).mean()
        dark = (np.mean(s, axis=0) < 30).mean()
        return moving + dark > 0.5

    span_x, span_y = int(0.2 * (r - l)), int(0.2 * (b - t))
    near_x, near_y = max(3, int(0.015 * W)), max(3, int(0.015 * H))
    # 1) moldura bem perto da borda: linha reta fortíssima (quase toda a altura) até ~1,5% para dentro
    if vline(r - 1) < 0.7:
        for x in range(r - 2, max(l, r - near_x), -1):
            if vline(x) >= 0.95:
                r = x + 1
                break
    if vline(l) < 0.7:
        for x in range(l + 1, min(r, l + near_x)):
            if vline(x) >= 0.95:
                l = x
                break
    if hline(b - 1) < 0.7:
        for y in range(b - 2, max(t, b - near_y), -1):
            if hline(y) >= 0.95:
                b = y + 1
                break
    if hline(t) < 0.7:
        for y in range(t + 1, min(b, t + near_y)):
            if hline(y) >= 0.95:
                t = y
                break
    # (encolher mais para dentro foi testado e removido: confundia gente se mexendo no quarto com jogo e
    # cortava a webcam; layouts difíceis usam a webcam fixa do cadastro do streamer)
    return (l, t, r, b)


MAX_CAM_AREA = 0.12  # webcam de streamer ocupa ~5-7% da tela; acima de 12% a caixa pegou jogo junto


def _area(b) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _grow_ok(new, old, W: int, H: int) -> bool:
    """A região parada só pode COMPLETAR a moldura (no máx. o dobro), nunca engolir parte do jogo."""
    return _area(new) <= 2.0 * _area(old) and _area(new) <= MAX_CAM_AREA * W * H


def _still_box(frames, cx: float, cy: float) -> tuple | None:
    """Painel da webcam pela parte da tela que fica PARADA em volta do rosto: o quarto do streamer muda pouco
    ao longo do corte, o jogo muda o tempo todo. Acha o painel inteiro mesmo quando as estantes e prateleiras
    do quarto confundem a busca por linhas retas. (cx, cy, frames e a caixa: na escala da análise.)"""
    import cv2
    if len(frames) < 6:
        return None
    H, W = frames[0].shape[:2]
    small = [cv2.cvtColor(cv2.resize(f, (W // 2, H // 2)), cv2.COLOR_BGR2GRAY).astype(np.float32) for f in frames]
    stack = np.stack(small)
    st = cv2.GaussianBlur(np.std(stack, axis=0), (5, 5), 0)
    # área escura também "não muda" (jogo à noite, mata, tela preta): não conta como fundo parado da webcam,
    # senão a moldura cresce para dentro do jogo
    lit = cv2.GaussianBlur(np.mean(stack, axis=0), (5, 5), 0) > 30
    still = ((st < 6) & lit).astype(np.uint8)
    if still.mean() > 0.6:
        return None  # jogo parado (menu, pausa): não dá para separar webcam e jogo
    still = cv2.morphologyEx(still, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))  # o rosto se mexe: fecha o buraco
    _, lab, stats, _ = cv2.connectedComponentsWithStats(still)
    fx, fy = int(cx / 2), int(cy / 2)
    best = 0
    for dy in range(-25, 26, 5):  # o rosto em si se mexe: procura o fundo parado em volta dele
        for dx in range(-25, 26, 5):
            y, x = min(max(fy + dy, 0), lab.shape[0] - 1), min(max(fx + dx, 0), lab.shape[1] - 1)
            if lab[y, x] and (best == 0 or stats[lab[y, x], 4] > stats[best, 4]):
                best = lab[y, x]
    if not best:
        return None
    x, y, w, h = (int(v) * 2 for v in stats[best, :4])
    area = w * h / (W * H)
    if not (0.01 <= area <= MAX_CAM_AREA) or not (0.6 <= w / max(h, 1) <= 2.6):
        return None
    if not (x <= cx <= x + w and y <= cy <= y + h):
        return None
    return (x, y, x + w, y + h)


def _union(a, b) -> tuple:
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _overlap(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    return ix * iy


def _inside(new: list[float], known: list[float]) -> bool:
    """A caixa nova está quase toda dentro da conhecida? Então é a mesma webcam detectada pela metade
    (erro de detecção, que corta a cabeça). Se a nova sai para fora, a webcam mudou de lugar de verdade."""
    ix = max(0.0, min(new[2], known[2]) - max(new[0], known[0]))
    iy = max(0.0, min(new[3], known[3]) - max(new[1], known[1]))
    area = max((new[2] - new[0]) * (new[3] - new[1]), 1.0)
    return ix * iy / area >= 0.9


def analyze(source: Path, start: float, end: float, src_w: int, src_h: int,
            known_cam: list[float] | None = None, locked: bool = False) -> dict:
    """Retorna {'kind': 'split'|'face'|'none', ...} com caixas em pixels do vídeo original.

    known_cam: moldura da webcam já achada antes (mesma live ou mesmo streamer). É usada quando
    o rosto não aparece no trecho, desde que as bordas dela continuem no lugar."""
    if locked and known_cam:  # webcam fixada no cadastro do streamer: usa direto, sem tentar adivinhar
        l, t, r, b = known_cam
        return {"kind": "split", "cam": [float(v) for v in known_cam], "cx": float(l + r) / 2, "reused": True}
    sh = _even(src_h * SW / src_w)
    k = src_w / SW
    try:
        frames = _sample(source, start, end, sh)
        if not frames:
            return {"kind": "none"}
        per_frame = _detect_faces(frames)
    except Exception as exc:
        print(f"[rosto] análise falhou: {exc}")
        return {"kind": "none"}

    if known_cam and _area(known_cam) > MAX_CAM_AREA * src_w * src_h:
        known_cam = None  # moldura guardada grande demais: pegou jogo junto, não serve de referência

    def from_known():
        try:
            if known_cam and _box_still_there(frames, known_cam, k):
                l, t, r, b = known_cam
                cam = [float(v) for v in known_cam]
                # a moldura conhecida pode ser só um pedaço do painel: completa com a região parada em volta
                kb = tuple(v / k for v in known_cam)
                sb = _still_box(frames, (kb[0] + kb[2]) / 2, (kb[1] + kb[3]) / 2)
                H0, W0 = frames[0].shape[:2]
                if sb and _overlap(sb, kb) >= 0.85 * _area(kb) and _grow_ok(_union(sb, kb), kb, W0, H0):
                    cam = [float(v * k) for v in _union(sb, kb)]
                cam = [float(v * k) for v in _snap_sides(frames, tuple(v / k for v in cam))]  # tira faixa de jogo
                return {"kind": "split", "cam": cam, "cx": float(l + r) / 2, "reused": True}
        except Exception as exc:
            print(f"[rosto] conferência da moldura falhou: {exc}")
        return None

    if known_cam:
        known = from_known()
        if known:
            return known

    per_frame = _stable_faces(per_frame, len(frames))
    with_faces = [fs for fs in per_frame if fs]
    if len(with_faces) < max(2, 0.3 * len(frames)):
        return {"kind": "none"}
    # caixa que envolve os rostos de cada quadro (webcam pode ter 2+ pessoas)
    boxes = np.array([[min(f[0] for f in fs), min(f[1] for f in fs),
                       max(f[0] + f[2] for f in fs), max(f[1] + f[3] for f in fs),
                       max(f[2] for f in fs)] for fs in with_faces], dtype=np.float32)
    cxs = (boxes[:, 0] + boxes[:, 2]) / 2
    if np.std(cxs) > 0.1 * SW:  # rostos pulando pela tela = personagens do jogo
        return from_known() or {"kind": "none"}
    fx0, fy0, fx1, fy1, fw = np.median(boxes, axis=0)

    if fw > 0.07 * SW:  # rosto grande: a pessoa é o assunto principal
        # trilha do rosto ao longo do corte, para o enquadramento acompanhar quem se mexe
        n = len(frames)
        dur = max(end - start, 1.0)
        track = []
        for i, fs in enumerate(per_frame):
            if fs:
                f = max(fs, key=lambda x: x[2] * x[3])
                track.append((i * dur / n, (f[0] + f[2] / 2) * k))
        return {"kind": "face", "cx": float((fx0 + fx1) / 2 * k), "track": track}

    box = _find_box(frames, fx0, fy0, fx1, fy1)
    if box is None:  # não achou a moldura: usa a conhecida, se ainda estiver lá
        known = from_known()
        if known:
            return known
    source = "edges"
    if box is None:  # senão, estima em volta dos rostos (com teto: webcam é pequena)
        source = "estimate"
        cw = min(max((fx1 - fx0) * 2.2, fw * 9), 0.4 * SW)
        ch = min(max((fy1 - fy0) * 3, cw * 9 / 16), 0.45 * sh)
        cx, cy = (fx0 + fx1) / 2, (fy0 + fy1) / 2
        box = (max(cx - cw / 2, 0), max(cy - ch * 0.45, 0), min(cx + cw / 2, SW), min(cy + ch * 0.55, sh))
        if (box[2] - box[0]) * (box[3] - box[1]) > 0.3 * SW * sh:
            return {"kind": "none"}
    # painel inteiro: junta a busca por bordas com a região parada em volta do rosto
    sb = _still_box(frames, (fx0 + fx1) / 2, (fy0 + fy1) / 2)
    if sb:
        if source == "edges" and _overlap(sb, box) > 0 and _grow_ok(_union(sb, box), box, SW, sh):
            box = _union(sb, box)
        elif source == "estimate":
            box, source = sb, "edges"  # a região parada é confiável: vira referência para os próximos cortes
    if source == "estimate":
        # caixa "chutada" em volta do rosto costuma pegar o jogo junto (o jogo aparece repetido em cima e
        # embaixo): melhor a moldura já confirmada do streamer ou o vídeo inteiro com fundo desfocado
        return from_known() or {"kind": "none"}
    if _area(box) > MAX_CAM_AREA * SW * sh:
        return from_known() or {"kind": "none"}  # moldura grande demais: pegou o jogo junto
    box = _snap_sides(frames, box)  # cada lado encostado numa borda real do painel
    l, t, r, b = (float(v * k) for v in box)
    if known_cam and _inside([l, t, r, b], known_cam):
        # a nova é um pedaço da moldura já confirmada (detecção pela metade): fica a confirmada inteira
        kl, kt, kr, kb = known_cam
        return {"kind": "split", "cam": [float(v) for v in known_cam], "cx": float(kl + kr) / 2, "reused": True}
    return {"kind": "split", "cam": [l, t, r, b], "cx": float((fx0 + fx1) / 2 * k), "source": source}


def choose(layout: str, info: dict, src_w: int, src_h: int) -> str:
    if src_h >= src_w:
        return "vertical"
    if layout == "auto":
        return {"split": "split", "face": "face"}.get(info["kind"], "blur")
    if layout == "split":
        return "split" if info["kind"] == "split" else ("face" if info["kind"] == "face" else "blur")
    if layout == "crop":
        return "face"
    return layout


def top_height(info: dict) -> int:
    l, t, r, b = info["cam"]
    return int(min(max(OUT_W * (b - t) / max(r - l, 1), 480), 860) // 2 * 2)


def video_filter(kind: str, info: dict, src_w: int, src_h: int) -> str:
    """Monta o filtro do ffmpeg (sem a legenda) que gera a imagem 1080x1920."""
    W, H = OUT_W, OUT_H
    if kind == "vertical":
        return f"[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2"

    if kind == "face":
        cw = _even(min(src_w, src_h * 9 / 16))
        x_expr = _follow_expr(info, cw, src_w)
        return f"[0:v]crop={cw}:{_even(src_h)}:'{x_expr}':0,scale={W}:{H}"

    if kind == "split":
        l, t, r, b = info["cam"]
        top_h = top_height(info)
        bot_h = H - top_h
        cam = f"crop={_even(r - l)}:{_even(b - t)}:{_even(l)}:{_even(t)},scale={W}:{top_h}:force_original_aspect_ratio=increase,crop={W}:{top_h}"
        # jogo: recorte central com a proporção do painel de baixo
        gw = _even(min(src_w, src_h * W / bot_h))
        gx = _even((src_w - gw) / 2)
        return (f"[0:v]split=2[c][g];[c]{cam}[top];"
                f"[g]crop={gw}:{_even(src_h)}:{gx}:0,scale={W}:{bot_h}[bot];"
                "[top][bot]vstack=inputs=2")

    return ("[0:v]split=2[a][b];"
            "[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=12:2,"
            f"eq=brightness=-0.12,scale={W}:{H}[bg];"
            f"[b]scale={W}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2")


def _follow_expr(info: dict, cw: int, src_w: int) -> str:
    """Expressão do ffmpeg para a posição x do recorte, movendo suave entre pontos-chave.

    Fica parado enquanto o rosto se mexe pouco (zona morta) e desliza quando ele sai do centro."""
    clamp = lambda c: float(min(max(c - cw / 2, 0), src_w - cw))  # noqa: E731
    track = info.get("track") or []
    if len(track) < 2:
        return str(_even(clamp(info.get("cx", src_w / 2))))
    ts = np.array([t for t, _ in track])
    xs = np.array([c for _, c in track])
    if len(xs) >= 5:  # mediana móvel tira detecções erradas isoladas
        xs = np.array([np.median(xs[max(0, i - 2):i + 3]) for i in range(len(xs))])
    keys = [(0.0, xs[0])]
    for t, c in zip(ts[1:], xs[1:]):
        if abs(c - keys[-1][1]) > cw * 0.18:  # só move se o rosto se afastou de verdade
            keys.append((float(t), float(c)))
    if len(keys) == 1:
        return str(_even(clamp(keys[0][1])))
    # Para cada mudança: fica em x[i-1] até "ta", desliza até x[i] em 0,8 s, e segue.
    # Montado de trás para frente: expr_i = if(t<ta_i, x[i-1], if(t<t_i, rampa_i, expr_{i+1}))
    expr = f"{clamp(keys[-1][1]):.1f}"
    for i in range(len(keys) - 1, 0, -1):
        t1 = keys[i][0]
        x0, x1 = clamp(keys[i - 1][1]), clamp(keys[i][1])
        ta = max(t1 - 0.8, keys[i - 1][0])
        span = max(t1 - ta, 0.01)
        ramp = f"({x0:.1f}+({x1 - x0:.1f})*(t-{ta:.2f})/{span:.2f})"
        expr = f"if(lt(t\\,{ta:.2f})\\,{x0:.1f}\\,if(lt(t\\,{t1:.2f})\\,{ramp}\\,{expr}))"
    return expr


def caption_margin(kind: str, info: dict) -> int:
    """Distância da legenda até a base do vídeo (a legenda fica logo abaixo da webcam no split)."""
    if kind == "split":
        return OUT_H - top_height(info) - 170
    return {"blur": 470, "face": 430, "vertical": 430}.get(kind, 450)
