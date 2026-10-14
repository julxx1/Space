"""Aislar el producto de una foto: quita el fondo y los demás objetos.

Motor 1 (preciso): modelo IS-Net / U2-Net con onnxruntime. El modelo se descarga solo,
                   una única vez, en la carpeta "modelos".
Motor 2 (básico):  relleno de fondo liso con Pillow (sirve para fotos de fondo blanco).

Si no hay motor disponible o el recorte no es fiable, la imagen original se conserva.
"""
import os
import threading
import time
import urllib.request
from collections import deque

BASE = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(BASE, "modelos")
RELEASE = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/"
MODELS = [
    {"name": "isnet-general-use", "size": 1024, "mean": (0.485, 0.456, 0.406),
     "std": (1.0, 1.0, 1.0), "min_bytes": 100_000_000},
    {"name": "u2net", "size": 320, "mean": (0.485, 0.456, 0.406),
     "std": (0.229, 0.224, 0.225), "min_bytes": 100_000_000},
]
RETRY_AFTER = 300  # segundos antes de reintentar una descarga fallida

STATE = {"engine": "idle", "model": "", "progress": 0, "error": ""}
_lock = threading.Lock()
_sess = None
_cfg = None
_failed_at = 0.0


def deps():
    """(Pillow disponible, motor de IA disponible)"""
    try:
        import PIL  # noqa: F401
        pil = True
    except Exception:
        pil = False
    try:
        import numpy  # noqa: F401
        import onnxruntime  # noqa: F401
        ml = True
    except Exception:
        ml = False
    return pil, ml


def status():
    pil, ml = deps()
    return dict(STATE, pillow=pil, onnx=ml, ready=_sess is not None)


def _download(cfg, path):
    os.makedirs(MODELS_DIR, exist_ok=True)
    url = RELEASE + cfg["name"] + ".onnx"
    part = path + ".part"
    STATE.update(engine="downloading", model=cfg["name"], progress=0, error="")
    req = urllib.request.Request(url, headers={"User-Agent": "MiCuarto/5"})
    with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if total:
                STATE["progress"] = int(got * 100 / total)
    if os.path.getsize(part) < cfg["min_bytes"]:
        os.remove(part)
        raise IOError("descarga incompleta")
    os.replace(part, path)


def ensure_engine():
    """Carga (y si hace falta descarga) el modelo. Devuelve True si hay IA disponible."""
    global _sess, _cfg, _failed_at
    with _lock:
        if _sess is not None:
            return True
        if _failed_at and time.time() - _failed_at < RETRY_AFTER:
            return False
        _, ml = deps()
        if not ml:
            STATE.update(engine="basic", error="Falta instalar onnxruntime y numpy.")
            _failed_at = time.time()
            return False
        import onnxruntime as ort
        last = ""
        for cfg in MODELS:
            path = os.path.join(MODELS_DIR, cfg["name"] + ".onnx")
            loading = False
            try:
                if not (os.path.isfile(path) and os.path.getsize(path) >= cfg["min_bytes"]):
                    _download(cfg, path)
                STATE.update(engine="loading", model=cfg["name"])
                loading = True
                so = ort.SessionOptions()
                so.intra_op_num_threads = max(1, min(4, os.cpu_count() or 2))
                _sess = ort.InferenceSession(path, so, providers=["CPUExecutionProvider"])
                _cfg = cfg
                STATE.update(engine="ready", model=cfg["name"], progress=100, error="")
                return True
            except Exception as e:  # red caída, disco lleno, archivo dañado...
                last = f"{cfg['name']}: {e.__class__.__name__}"
                if loading:  # el archivo existe pero no carga: está dañado, se vuelve a bajar luego
                    try:
                        os.remove(path)
                    except OSError:
                        pass
        STATE.update(engine="basic", error="No pude preparar la IA (" + last + "). Usando modo básico.")
        _failed_at = time.time()
        return False


def prefetch():
    threading.Thread(target=ensure_engine, daemon=True).start()


def reset_retry():
    global _failed_at
    _failed_at = 0.0


# ------------------------------------------------------------------ imagen
def _predict(img):
    import numpy as np
    from PIL import Image
    s = _cfg["size"]
    a = np.asarray(img.resize((s, s), Image.LANCZOS), dtype=np.float32)
    a = a / max(float(a.max()), 1e-6)
    a = (a - np.array(_cfg["mean"], dtype=np.float32)) / np.array(_cfg["std"], dtype=np.float32)
    a = a.transpose(2, 0, 1)[None, ...].astype(np.float32)
    out = _sess.run(None, {_sess.get_inputs()[0].name: a})[0]
    p = out[0, 0]
    mn, mx = float(p.min()), float(p.max())
    p = (p - mn) / max(mx - mn, 1e-6)
    m = Image.fromarray((p * 255).astype("uint8")).resize(img.size, Image.LANCZOS)
    return np.asarray(m, dtype=np.float32) / 255.0


def _components(binary):
    """Componentes conectados (4 vecinos) de una matriz booleana. -> lista de listas de (y, x)"""
    rows = binary.tolist()
    h, w = len(rows), len(rows[0])
    seen = [[False] * w for _ in range(h)]
    comps = []
    for y in range(h):
        for x in range(w):
            if rows[y][x] and not seen[y][x]:
                seen[y][x] = True
                q, cells = deque([(y, x)]), []
                while q:
                    cy, cx = q.popleft()
                    cells.append((cy, cx))
                    for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                        if 0 <= ny < h and 0 <= nx < w and rows[ny][nx] and not seen[ny][nx]:
                            seen[ny][nx] = True
                            q.append((ny, nx))
                comps.append(cells)
    return comps


def _region(mask):
    """Zona (0/1, mismo tamaño que mask) del objeto principal: descarta motas y objetos
    secundarios pequeños, y rellena huecos diminutos."""
    import numpy as np
    from PIL import Image, ImageFilter
    H, W = mask.shape
    k = min(1.0, 160.0 / max(H, W))
    sw, sh = max(1, int(W * k)), max(1, int(H * k))
    small = np.asarray(Image.fromarray((mask * 255).astype("uint8")).resize((sw, sh), Image.BILINEAR)) > 110
    comps = _components(small)
    if not comps:
        return None
    cx0, cy0 = sw / 2, sh / 2
    half_diag = (cx0 ** 2 + cy0 ** 2) ** 0.5

    def score(cells):
        a = len(cells)
        my = sum(c[0] for c in cells) / a
        mx = sum(c[1] for c in cells) / a
        d = (((mx - cx0) ** 2 + (my - cy0) ** 2) ** 0.5) / half_diag
        return a * (1.0 - 0.5 * d)

    main = max(comps, key=score)
    keep = np.zeros((sh, sw), dtype=bool)
    for cells in comps:
        if len(cells) >= 0.30 * len(main):
            for y, x in cells:
                keep[y, x] = True
    # huecos pequeños dentro del objeto (ruido de la máscara) se rellenan
    holes = _components(~keep)
    for cells in holes:
        touches = any(y in (0, sh - 1) or x in (0, sw - 1) for y, x in cells)
        if not touches and len(cells) < 0.008 * sw * sh:
            for y, x in cells:
                keep[y, x] = True
    reg = Image.fromarray((keep * 255).astype("uint8")).filter(ImageFilter.MaxFilter(3))
    reg = reg.resize((W, H), Image.BILINEAR)
    return (np.asarray(reg, dtype=np.float32) / 255.0) > 0.05


def _basic_mask(img):
    """Fondo liso -> máscara L (255 = producto). None si el fondo no es liso."""
    from PIL import Image, ImageChops, ImageDraw
    w, h = img.size
    k = min(1.0, 700.0 / max(w, h))
    small = img.resize((max(1, int(w * k)), max(1, int(h * k))), Image.LANCZOS) if k < 1 else img.copy()
    sw, sh = small.size
    px = small.load()
    border = [px[x, 0] for x in range(0, sw, 3)] + [px[x, sh - 1] for x in range(0, sw, 3)] + \
             [px[0, y] for y in range(0, sh, 3)] + [px[sw - 1, y] for y in range(0, sh, 3)]
    med = tuple(sorted(c[i] for c in border)[len(border) // 2] for i in range(3))
    tol = 26
    close = [c for c in border if all(abs(c[i] - med[i]) <= tol for i in range(3))]
    if len(close) / len(border) < 0.85:
        return None
    sentinel = (255, 0, 255)
    filled = small.copy()
    seeds = [(x, 0) for x in range(0, sw, max(1, sw // 12))] + [(x, sh - 1) for x in range(0, sw, max(1, sw // 12))] + \
            [(0, y) for y in range(0, sh, max(1, sh // 12))] + [(sw - 1, y) for y in range(0, sh, max(1, sh // 12))]
    for (x, y) in seeds:
        c = filled.getpixel((x, y))
        if c != sentinel and all(abs(c[i] - med[i]) <= tol for i in range(3)):
            ImageDraw.floodfill(filled, (x, y), sentinel, thresh=tol)
    diff = ImageChops.difference(filled, Image.new("RGB", small.size, sentinel)).convert("L")
    mask = diff.point(lambda v: 255 if v > 0 else 0)
    return mask.resize((w, h), Image.BILINEAR)


def _finish(img, alpha_l, dst):
    """Aplica la transparencia, recorta al producto y centra en un lienzo cuadrado."""
    from PIL import Image, ImageFilter
    alpha_l = alpha_l.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(0.8))
    bbox = alpha_l.point(lambda v: 255 if v > 24 else 0).getbbox()
    if not bbox:
        return False
    out = img.convert("RGBA")
    out.putalpha(alpha_l)
    x0, y0, x1, y1 = bbox
    pad = int(0.06 * max(x1 - x0, y1 - y0)) + 2
    x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
    x1, y1 = min(img.width, x1 + pad), min(img.height, y1 + pad)
    out = out.crop((x0, y0, x1, y1))
    side = max(out.size)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(out, ((side - out.width) // 2, (side - out.height) // 2))
    if side > 1024:
        canvas = canvas.resize((1024, 1024), Image.LANCZOS)
    canvas.save(dst, "PNG", optimize=True)
    return True


def isolate(src, dst):
    """Deja solo el producto de la imagen `src` y lo guarda como PNG transparente en `dst`.
    -> {"ok": bool, "method": str, "note": str}"""
    try:
        from PIL import Image, ImageOps
    except Exception:
        return {"ok": False, "method": "", "note": "Falta instalar Pillow."}
    try:
        img = Image.open(src)
        img.load()
        img = ImageOps.exif_transpose(img)
    except Exception:
        return {"ok": False, "method": "", "note": "Formato de imagen no compatible."}

    # ya trae transparencia real (esquinas transparentes): solo recortar
    if img.mode in ("RGBA", "LA") or "transparency" in img.info:
        rgba = img.convert("RGBA")
        a = rgba.getchannel("A")
        corners = [a.getpixel(p) for p in ((0, 0), (a.width - 1, 0), (0, a.height - 1), (a.width - 1, a.height - 1))]
        if max(corners) < 16:
            ok = _finish(rgba.convert("RGB"), a, dst)
            return {"ok": ok, "method": "transparente", "note": "La imagen ya tenía fondo transparente."}
        bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        bg.alpha_composite(rgba)
        img = bg
    img = img.convert("RGB")
    if max(img.size) > 1600:
        k = 1600.0 / max(img.size)
        img = img.resize((int(img.width * k), int(img.height * k)), Image.LANCZOS)

    alpha, method = None, ""
    if ensure_engine():
        try:
            import numpy as np
            mask = _predict(img)
            reg = _region(mask)
            if reg is not None:
                t = np.clip((mask - 0.15) / 0.70, 0.0, 1.0) * reg
                alpha = Image.fromarray((t * 255).astype("uint8"))
                method = "ia"
        except Exception as e:
            STATE["error"] = f"Recorte con IA falló: {e.__class__.__name__}"
    if alpha is None:
        m = _basic_mask(img)
        if m is not None:
            alpha, method = m, "basico"
    if alpha is None:
        return {"ok": False, "method": "", "note": "No pude separar el producto con seguridad; se dejó la imagen original."}

    cover = sum(alpha.point(lambda v: 255 if v > 127 else 0).histogram()[255:]) / float(alpha.width * alpha.height)
    if cover < 0.02:
        return {"ok": False, "method": method, "note": "No detecté un producto claro; se dejó la imagen original."}
    if cover > 0.985:
        return {"ok": False, "method": method, "note": "La imagen no tenía fondo que quitar."}
    if not _finish(img, alpha, dst):
        return {"ok": False, "method": method, "note": "No pude recortar la imagen."}
    return {"ok": True, "method": method, "note": ""}
