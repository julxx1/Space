"""Mi Cuarto - guarda ideas de decoración pegando solo un link.
Corre en tu PC: http://localhost:8765

Archivos:  server.py (este), cutout.py (aislar producto), prices.py (comparar precios), index.html
Datos:     data.json (ideas), groups.json (moodboards y colecciones), settings.json, imagenes/, modelos/
"""
import base64, hashlib, json, os, re, threading, unicodedata, uuid, webbrowser, html as htmlmod
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from html.parser import HTMLParser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

import cutout
import prices

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data.json")
GROUPS = os.path.join(BASE, "groups.json")
SETTINGS = os.path.join(BASE, "settings.json")
IMG = os.path.join(BASE, "imagenes")
prices.RATES_FILE = os.path.join(BASE, "rates.json")
PORT = int(os.environ.get("MICUARTO_PORT", "8765"))
MAX_IMGS = 12
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
UAS = [
    UA,
    "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uagent.php)",
    "Twitterbot/1.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
]
GENERIC = {"amazon.com", "amazon.es", "amazon.com.mx", "amazon.com.br", "amazon.co.uk",
           "amazon.de", "pinterest", "access denied", "just a moment...", "robot check",
           "attention required! | cloudflare", "403 forbidden", "page not found"}
CATEGORIES = ["Iluminación", "Muebles", "Decoración", "Textiles", "Organización",
              "Arte y posters", "Plantas", "Espejos", "Otros"]
KEYWORDS = [  # el orden es la prioridad
    ("Espejos", "espejo mirror"),
    ("Plantas", "planta plantas maceta matera cactus suculenta helecho plant planter flor flores"),
    ("Iluminación", "lampara lamparas lamp luz luces led bombillo bombilla foco aplique neon velador lampshade guirnalda light lights"),
    ("Arte y posters", "poster posters cuadro cuadros lamina laminas marco marcos canvas frame tapiz mural pintura fotografia print"),
    ("Textiles", "cojin cojines almohada manta cobija cortina cortinas alfombra tapete funda sabana edredon cobertor pillow rug curtain blanket throw duvet textil"),
    ("Organización", "estante estanteria estanterias repisa organizador organizadores caja cajas cesta canasta gancho percha perchero shelf shelves storage basket organizer bin cajon"),
    ("Muebles", "silla sillas sillon sofa mesa mesas escritorio cama comoda mueble muebles banco banca puff pouf butaca closet armario rack chair desk table couch ottoman nightstand mesita tocador"),
    ("Decoración", "adorno adornos figura figuras vela velas jarron florero decor decoracion reloj escultura candle vase ornament bandeja"),
]
DEFAULT_SETTINGS = {"isolate": True, "auto_compare": True, "country": "CO", "currency": "", "serper_key": ""}
DEFAULT_GROUPS = [("mb-cuarto", "Cuarto principal"), ("mb-escritorio", "Escritorio"),
                  ("mb-closet", "Closet"), ("mb-lectura", "Rincón de lectura")]

lock = threading.RLock()
CUT_POOL = ThreadPoolExecutor(1)   # el recorte usa mucha CPU: de uno en uno
NET_POOL = ThreadPoolExecutor(3)   # consultas de precios
os.makedirs(IMG, exist_ok=True)


# ============================================================ datos
def read_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn")


def categorize(text):
    toks = set(re.findall(r"[a-z0-9]+", strip_accents(text).lower()))
    for cat, words in KEYWORDS:
        for w in words.split():
            if w in toks or (len(w) >= 5 and any(t.startswith(w) for t in toks)):
                return cat
    return "Otros"


def norm(it):
    """Compatibilidad hacia atrás: completa los campos que las ideas antiguas no tenían."""
    imgs = it.get("images")
    if imgs is None:
        imgs = [it["image"]] if it.get("image") else []
    it["images"] = [i for i in imgs if i]
    it["image"] = it["images"][0] if it["images"] else ""
    it.setdefault("alts", {})
    it.setdefault("iso", [])
    it.setdefault("srcs", {})
    it.setdefault("groups", [])
    it.setdefault("shop", False)
    it.setdefault("bought", False)
    it.setdefault("busy", {})
    if not it.get("category") or it["category"] not in CATEGORIES:
        it["category"] = categorize((it.get("name") or "") + " " + urlparse(it.get("url") or "").path)
    return it


def load():
    return [norm(i) for i in read_json(DATA, [])]


def save(items):
    write_json(DATA, items)


def get_settings():
    s = dict(DEFAULT_SETTINGS)
    s.update({k: v for k, v in read_json(SETTINGS, {}).items() if k in DEFAULT_SETTINGS})
    return s


def load_groups():
    if not os.path.exists(GROUPS):
        g = [{"id": i, "kind": "moodboard", "name": n, "created": datetime.now().isoformat(timespec="seconds")}
             for i, n in DEFAULT_GROUPS]
        write_json(GROUPS, g)
        return g
    return read_json(GROUPS, [])


def effective_currency(s=None):
    s = s or get_settings()
    if s.get("currency") in prices.CURRENCIES:
        return s["currency"]
    return prices.COUNTRIES.get(s.get("country"), prices.COUNTRIES["CO"])["cur"]


def public_settings():
    s = get_settings()
    key = s.pop("serper_key", "")
    s["has_key"] = bool(key)
    s["key_hint"] = ("••••" + key[-4:]) if key else ""
    s["countries"] = {k: v["name"] for k, v in prices.COUNTRIES.items()}
    s["currencies"] = prices.CURRENCIES
    s["currency_eff"] = effective_currency(s)
    return s


def find(items, iid):
    return next((i for i in items if i["id"] == iid), None)


def update_item(iid, fn):
    """Aplica fn(item) con el candado puesto y guarda. -> item o None"""
    with lock:
        items = load()
        it = find(items, iid)
        if not it:
            return None
        fn(it)
        norm(it)
        save(items)
        return it


# ============================================================ red / páginas
def fetch(url, referer=None, limit=15_000_000, ua=None):
    headers = {
        "User-Agent": ua or UA,
        "Accept": "text/html,application/xhtml+xml,image/*;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-CO,es;q=0.9,en;q=0.8",
        "Accept-Encoding": "identity",
    }
    if referer:
        headers["Referer"] = referer
    if re.search(r"//[^/]*amazon\.com(/|$)", url):   # que Amazon muestre el precio en tu moneda
        headers["Cookie"] = f"i18n-prefs={effective_currency()}; lc-main=es_US"
    with urlopen(Request(url, headers=headers), timeout=15) as r:
        body = r.read(limit)
        return body, r.headers.get("Content-Type", ""), r.geturl(), r.headers


IMG_KEYS = ("og:image", "og:image:url", "og:image:secure_url",
            "twitter:image", "twitter:image:src", "image")


class Meta(HTMLParser):
    def __init__(self):
        super().__init__()
        self.meta, self.title, self._t = {}, "", False
        self.metaimgs, self.imgs = [], []
        self.ld, self._ld = [], False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            k = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if k and a.get("content"):
                self.meta.setdefault(k, a["content"])
                if k in IMG_KEYS:
                    self.metaimgs.append(a["content"])
        elif tag == "link" and (a.get("rel") or "").lower() == "image_src" and a.get("href"):
            self.metaimgs.append(a["href"])
        elif tag == "title":
            self._t = True
        elif tag == "script" and "ld+json" in (a.get("type") or ""):
            self._ld = True
            self.ld.append("")
        elif tag == "img":  # Amazon y similares
            for k in ("data-old-hires", "data-a-hires"):
                if a.get(k):
                    self.imgs.append(a[k])
            if a.get("data-a-dynamic-image"):
                try:
                    self.imgs.extend(json.loads(htmlmod.unescape(a["data-a-dynamic-image"])).keys())
                except Exception:
                    pass
            if a.get("id") == "landingImage" and a.get("src"):
                self.imgs.append(a["src"])

    def handle_endtag(self, tag):
        if tag == "title":
            self._t = False
        elif tag == "script":
            self._ld = False

    def handle_data(self, d):
        if self._t:
            self.title += d
        if self._ld and self.ld:
            self.ld[-1] += d


def ld_images(texts):
    out = []

    def grab(v):
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, list):
            for i in v:
                grab(i)
        elif isinstance(v, dict):
            grab(v.get("url") or v.get("contentUrl") or "")

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                grab(v) if k == "image" else walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    for t in texts:
        try:
            walk(json.loads(t))
        except Exception:
            pass
    return out


def decode(body, ctype):
    m = re.search(r"charset=([\w-]+)", ctype or "", re.I) or re.search(
        rb"<meta[^>]+charset=[\"']?([\w-]+)", body[:5000], re.I)
    enc = "utf-8"
    if m:
        g = m.group(1)
        enc = g.decode() if isinstance(g, bytes) else g
    try:
        return body.decode(enc, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def clean_title(t, host):
    t = htmlmod.unescape(re.sub(r"\s+", " ", t or "")).strip()
    if "amazon" in host:
        t = re.sub(r"^Amazon\.[\w.]+\s*:\s*", "", t)
    return t[:200]


def asin_of(url):
    m = re.search(r"/(?:dp|gp/product|gp/aw/d|product)/([A-Z0-9]{10})(?:[/?]|$)", url)
    return m.group(1) if m else ""


def parse_page(url, ua):
    body, ctype, final, _ = fetch(url, ua=ua)
    text = decode(body, ctype)
    p = Meta()
    try:
        p.feed(text)
    except Exception:
        pass
    host = urlparse(final).netloc.lower()
    name = clean_title((p.meta.get("og:title") or p.meta.get("twitter:title") or p.title or "").strip(), host)
    if name.strip().lower() in GENERIC or re.search(r"captcha|robot check", name, re.I):
        name = ""
    imgs = []
    for u in p.metaimgs + ld_images(p.ld) + p.imgs:
        u = urljoin(final, htmlmod.unescape((u or "").strip()))
        if u.startswith("http") and u not in imgs:
            imgs.append(u)
    price = None
    try:
        offers = prices.extract_offers(text, final)
        if offers:
            cur = offers[0].get("currency") or prices.guess_currency(
                final, prices.COUNTRIES.get(get_settings()["country"], prices.COUNTRIES["CO"])["cur"])
            price = {"amount": offers[0]["price"], "currency": cur.upper()}
            if offers[0].get("approx"):
                price["approx"] = True   # leído del HTML de la tienda: sin confirmar
    except Exception:
        pass
    return name, imgs[:8], price


def scrape(url):
    """-> (nombre, [urls de imagen], error, precio). Reintenta con distintos 'visitantes'."""
    name, imgs, err, price = "", [], None, None
    for ua in UAS:
        try:
            n, i, pr = parse_page(url, ua)
        except Exception as e:
            err = e
            continue
        name = name or n
        price = price or pr
        imgs += [u for u in i if u not in imgs]
        if name and imgs and price:
            break
    asin = asin_of(url)
    if not imgs and asin and "amazon" in urlparse(url).netloc.lower():
        imgs = [f"https://m.media-amazon.com/images/P/{asin}.01.LZZZZZZZ.jpg"]
    return name, imgs, err, price


# ============================================================ imágenes
EXTS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
        "image/gif": ".gif", "image/avif": ".avif"}


def fetch_image(img_url, referer):
    try:
        body, ctype, _, _ = fetch(img_url, referer=referer)
    except Exception:
        return None
    if not body or "text/html" in ctype or len(body) < 1500:
        return None
    ext = EXTS.get(ctype.split(";")[0].strip(), "")
    if not ext:
        e = os.path.splitext(urlparse(img_url).path)[1].lower()
        ext = e if e in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif") else ".jpg"
    return body, ext


def store(body, ext):
    fn = uuid.uuid4().hex[:12] + ext
    with open(os.path.join(IMG, fn), "wb") as f:
        f.write(body)
    return fn


def download_image(img_url, referer):
    r = fetch_image(img_url, referer)
    return store(*r) if r else ""


def download_many(urls, referer, limit=6, srcs=None):
    urls = urls[: limit + 3]
    if not urls:
        return []
    with ThreadPoolExecutor(4) as ex:
        res = list(ex.map(lambda u: fetch_image(u, referer), urls))
    seen, out = set(), []
    for u, r in zip(urls, res):
        if not r:
            continue
        h = hashlib.sha1(r[0]).hexdigest()
        if h in seen:
            continue
        seen.add(h)
        out.append(store(*r))
        if srcs is not None:
            srcs[out[-1]] = u
        if len(out) >= limit:
            break
    return out


def remove_image(fn):
    if fn:
        try:
            os.remove(os.path.join(IMG, os.path.basename(fn)))
        except OSError:
            pass


def drop_image(it, fn):
    """Quita una imagen (y su versión alternativa) de la idea y borra los archivos."""
    other = it["alts"].pop(fn, None)
    if other:
        it["alts"].pop(other, None)
        remove_image(other)
    if fn in it["images"]:
        it["images"].remove(fn)
    it["iso"] = [i for i in it["iso"] if i not in (fn, other)]
    remove_image(fn)


def swap_image(it, fn):
    """Alterna una imagen entre su versión original y la de 'solo producto'."""
    other = it["alts"].pop(fn, None)
    if not other or fn not in it["images"]:
        return False
    it["images"][it["images"].index(fn)] = other
    it["alts"][other] = fn
    return True


# ============================================================ trabajos en segundo plano
def job_isolate(iid, only=None):
    try:
        with lock:
            it = find(load(), iid)
            if not it:
                return
            todo = [f for f in it["images"] if (only is None or f == only)
                    and f not in it["iso"] and f not in it["alts"]]
        made = []
        for fn in todo:
            new = uuid.uuid4().hex[:12] + ".png"
            r = cutout.isolate(os.path.join(IMG, fn), os.path.join(IMG, new))
            made.append((fn, new if r["ok"] else "", r))

        def apply(it):
            notes, okc = [], 0
            for fn, new, r in made:
                if new and fn in it["images"]:
                    it["images"][it["images"].index(fn)] = new
                    it["alts"][new] = fn
                    it["iso"].append(new)
                    okc += 1
                elif new:
                    remove_image(new)  # la imagen se quitó mientras se procesaba
                elif r.get("note"):
                    notes.append(r["note"])
            it["busy"].pop("cutout", None)
            it["cut_note"] = "" if okc or not notes else notes[0]

        if update_item(iid, apply) is None:
            for _, new, _ in made:
                remove_image(new)
    except Exception as e:
        update_item(iid, lambda it: (it["busy"].pop("cutout", None), it.update(cut_note="Error al aislar: " + e.__class__.__name__)))


def job_compare(iid, query=None):
    try:
        with lock:
            it = find(load(), iid)
            if not it:
                return
            name, url, price = it["name"], it["url"], it.get("price")
            first = it["images"][0] if it["images"] else ""
            itc = dict(it)
        if not query and "pinterest" in (url or "") and first and get_settings().get("serper_key"):
            try:  # un pin no dice qué producto es: lo identifico por la foto y busco con ese nombre
                im = run_imgsearch(itc, first)
                update_item(iid, lambda i: i.update(imgsearch=im))
                query = im.get("guess") or None
            except Exception:
                pass
        if url and not (price and price.get("manual")):  # precio no confirmado por ti: se vuelve a leer
            try:
                fresh = scrape(url)[3]
                if fresh:
                    price = fresh
                elif price:
                    price = dict(price, approx=True)   # no pude releerlo: queda sin confirmar
                if price:
                    update_item(iid, lambda it: it.update(price=price))
            except Exception:
                pass
        res = prices.compare(name, url, price, get_settings(), query)
    except Exception as e:
        res = {"error": "No pude comparar precios (" + e.__class__.__name__ + ").",
               "checked_at": datetime.now().isoformat(timespec="seconds"),
               "links": prices.quick_links(prices.clean_query(name), get_settings().get("country", "CO")),
               "offers": [], "best": None, "notes": [], "summary": ""}

    def apply(it):
        it["compare"] = res
        it["busy"].pop("compare", None)

    update_item(iid, apply)


def queue_isolate(iid, only=None):
    update_item(iid, lambda it: it["busy"].__setitem__("cutout", True))
    CUT_POOL.submit(job_isolate, iid, only)


def public_src(it, fn):
    """Link público de la foto original (lo necesita la búsqueda por imagen)."""
    s = it.get("srcs") or {}
    return s.get(fn) or s.get(it["alts"].get(fn, "")) or ""


def run_imgsearch(it, fn):
    st = get_settings()
    key = (st.get("serper_key") or "").strip()
    out = {"image": fn, "checked_at": datetime.now().isoformat(timespec="seconds")}
    src = public_src(it, fn)
    if not key:
        out["error"] = "Para analizar la foto automáticamente necesitas la clave gratuita de Serper.dev (Configuración)."
    elif not src:
        out["error"] = "Esta foto no tiene un link público (la subiste o pegaste tú). Usa una foto que venga de la página."
    else:
        try:
            out.update(prices.image_search(src, st.get("country"), key, effective_currency(st)))
        except Exception as e:
            out["error"] = "No pude buscar la foto (" + e.__class__.__name__ + "). Revisa tu clave de Serper."
    return out


def job_imgsearch(iid, fn):
    try:
        with lock:
            it = find(load(), iid)
            if not it:
                return
        res = run_imgsearch(it, fn)
    except Exception as e:
        res = {"image": fn, "error": "No pude analizar la foto (" + e.__class__.__name__ + ")."}

    def apply(it):
        it["imgsearch"] = res
        it["busy"].pop("imgsearch", None)

    update_item(iid, apply)


def queue_compare(iid, query=None):
    update_item(iid, lambda it: it["busy"].__setitem__("compare", True))
    NET_POOL.submit(job_compare, iid, query)


# ============================================================ servidor HTTP
class Bad(Exception):
    pass


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, obj=None, raw=None, ctype="application/json; charset=utf-8"):
        data = raw if raw is not None else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 40_000_000:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def guard(self, fn):
        try:
            fn()
        except Bad as e:
            self.send(400, {"error": str(e)})
        except Exception as e:  # nunca dejar la página colgada
            try:
                self.send(500, {"error": "Error interno: " + e.__class__.__name__})
            except Exception:
                pass

    def parts(self):
        return [p for p in self.path.split("?")[0].split("/") if p]

    # ---------------------------------------------------------------- GET
    def do_GET(self):
        self.guard(self._get)

    def _get(self):
        p = self.parts()
        if not p:
            with open(os.path.join(BASE, "index.html"), "rb") as f:
                return self.send(200, raw=f.read(), ctype="text/html; charset=utf-8")
        if p == ["api", "items"]:
            with lock:
                return self.send(200, load())
        if p == ["api", "groups"]:
            with lock:
                return self.send(200, load_groups())
        if p == ["api", "settings"]:
            return self.send(200, public_settings())
        if len(p) == 2 and p[0] == "lens":
            fn = os.path.basename(p[1])
            page = ("<!doctype html><meta charset=utf-8><title>Buscando…</title><body style='font:16px system-ui;padding:40px'>"
                    "<p>Subiendo tu foto a Google Lens…</p><p style='color:#666;font-size:14px'>Si no se abre, entra a lens.google.com y arrastra la foto.</p>"
                    "<form id=f method=POST action='https://lens.google.com/upload?hl=es' enctype='multipart/form-data'><input type=file name=encoded_image id=i></form>"
                    "<script>fetch('/imagenes/" + fn + "').then(r=>r.blob()).then(b=>{var d=new DataTransfer();d.items.add(new File([b],'foto.jpg',{type:b.type||'image/jpeg'}));"
                    "document.getElementById('i').files=d.files;document.getElementById('f').submit()})</script>")
            return self.send(200, raw=page.encode("utf-8"), ctype="text/html; charset=utf-8")
        if p == ["api", "rates"]:
            r, at = prices.get_rates()
            return self.send(200, {"base": "USD", "rates": r, "updated": at})
        if p == ["api", "status"]:
            with lock:
                busy = sum(1 for i in load() if i["busy"])
            return self.send(200, {"engine": cutout.status(), "busy": busy,
                                   "categories": CATEGORIES})
        if p[0] == "imagenes" and len(p) == 2:
            fp = os.path.join(IMG, os.path.basename(p[1]))
            if os.path.isfile(fp):
                ext = os.path.splitext(fp)[1].lower().lstrip(".")
                ct = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
                with open(fp, "rb") as f:
                    return self.send(200, raw=f.read(), ctype=ct)
        self.send(404, {"error": "no encontrado"})

    # ---------------------------------------------------------------- POST
    def do_POST(self):
        self.guard(self._post)

    def _post(self):
        p, b = self.parts(), self.body()
        if p == ["api", "items"]:
            return self.create_item(b)
        if p == ["api", "groups"]:
            name = (b.get("name") or "").strip()[:60]
            kind = b.get("kind") if b.get("kind") in ("moodboard", "coleccion") else "coleccion"
            if not name:
                raise Bad("Ponle un nombre.")
            with lock:
                g = load_groups()
                new = {"id": uuid.uuid4().hex[:8], "kind": kind, "name": name,
                       "created": datetime.now().isoformat(timespec="seconds")}
                g.append(new)
                write_json(GROUPS, g)
            return self.send(200, new)
        if len(p) == 4 and p[:2] == ["api", "groups"] and p[3] == "items":
            gid = p[2]
            with lock:
                if not any(g["id"] == gid for g in load_groups()):
                    raise Bad("Ese grupo no existe.")
                items = load()
                add, rem = set(b.get("add") or []), set(b.get("remove") or [])
                for it in items:
                    if it["id"] in add and gid not in it["groups"]:
                        it["groups"].append(gid)
                    if it["id"] in rem and gid in it["groups"]:
                        it["groups"].remove(gid)
                save(items)
            return self.send(200, {"ok": True})
        if len(p) == 4 and p[:2] == ["api", "items"]:
            return self.item_action(p[2], p[3], b)
        if p == ["api", "isolate_all"]:
            return self.isolate_all(b.get("mode"))
        if p == ["api", "engine", "retry"]:
            cutout.reset_retry()
            cutout.prefetch()
            return self.send(200, cutout.status())
        self.send(404, {"error": "no encontrado"})

    def create_item(self, b):
        url = (b.get("url") or "").strip()
        if not re.match(r"^https?://", url):
            if re.match(r"^[\w.-]+\.\w{2,}", url):
                url = "https://" + url
            else:
                raise Bad("Eso no parece un link válido.")
        settings = get_settings()
        name = (b.get("title") or "").strip()[:200]
        imgs = [u for u in (b.get("images") or []) if isinstance(u, str) and u.startswith("http")]
        if b.get("image_url") and b["image_url"] not in imgs:
            imgs.insert(0, b["image_url"])
        cur0 = prices.COUNTRIES.get(settings["country"], prices.COUNTRIES["CO"])["cur"]
        price = None
        pt = b.get("price_text")
        if pt and prices.parse_price(pt):  # precio leído por el botón de favoritos
            price = {"manual": True, "amount": prices.parse_price(pt), "currency": prices.currency_of(pt, prices.guess_currency(url, cur0))}
        err = None
        if not (name and imgs):
            n2, i2, err, p2 = scrape(url)
            name = name or n2
            price = price or p2
            imgs += [u for u in i2 if u not in imgs]
        srcs = {}
        files = download_many(imgs, url, srcs=srcs)
        if not name and not files and err is not None:
            aviso = f"No pude abrir la página ({err.__class__.__name__}). Complétalo tú."
        else:
            faltan = ("nombre e imagen" if not name and not files else "el nombre" if not name
                      else "la imagen" if not files else "")
            aviso = f"No pude obtener {faltan} de esta página. Complétalo tú." if faltan else ""
        host = urlparse(url).netloc.replace("www.", "")
        item = norm({
            "id": uuid.uuid4().hex[:10],
            "name": name or host,
            "images": files,
            "srcs": srcs,
            "url": url,
            "site": host,
            "added": datetime.now().isoformat(timespec="seconds"),
            "price": price,
            "category": categorize((name or "") + " " + urlparse(url).path),
        })
        if settings["isolate"] and files:
            item["busy"]["cutout"] = True
        if settings["auto_compare"] and name:
            item["busy"]["compare"] = True
        with lock:
            items = load()
            items.insert(0, item)
            save(items)
        if item["busy"].get("cutout"):
            CUT_POOL.submit(job_isolate, item["id"], None)
        if item["busy"].get("compare"):
            NET_POOL.submit(job_compare, item["id"], None)
        self.send(200, {"item": item, "aviso": aviso})

    def item_action(self, iid, action, b):
        with lock:
            it = find(load(), iid)
        if not it:
            return self.send(404, {"error": "no encontrado"})
        if action == "isolate":
            if not it["images"]:
                raise Bad("Esta idea no tiene imágenes.")
            queue_isolate(iid, b.get("image"))
        elif action == "swap":
            fn = b.get("image")
            if update_item(iid, lambda i: swap_image(i, fn)) is None:
                return self.send(404, {"error": "no encontrado"})
        elif action == "restore":
            def restore(i):
                for fn in list(i["images"]):
                    if fn in i["iso"]:
                        swap_image(i, fn)
            update_item(iid, restore)
        elif action == "imgsearch":
            fn = b.get("image") or (it["images"][0] if it["images"] else "")
            if not fn:
                raise Bad("Esta idea no tiene imágenes.")
            update_item(iid, lambda i: i["busy"].__setitem__("imgsearch", True))
            NET_POOL.submit(job_imgsearch, iid, fn)
        elif action == "compare":
            q = (b.get("query") or "").strip()[:150] or None
            queue_compare(iid, q)
        else:
            return self.send(404, {"error": "no encontrado"})
        with lock:
            return self.send(200, find(load(), iid))

    def isolate_all(self, mode):
        with lock:
            items = load()
        n = 0
        for it in items:
            if mode == "restore":
                def restore(i):
                    for fn in list(i["images"]):
                        if fn in i["iso"]:
                            swap_image(i, fn)
                update_item(it["id"], restore)
                n += 1
            elif it["images"] and any(f not in it["iso"] and f not in it["alts"] for f in it["images"]):
                queue_isolate(it["id"])
                n += 1
        self.send(200, {"count": n})

    # ---------------------------------------------------------------- PUT (ajustes)
    def do_PUT(self):
        self.guard(self._put)

    def _put(self):
        if self.parts() != ["api", "settings"]:
            return self.send(404, {"error": "no encontrado"})
        b = self.body()
        with lock:
            s = get_settings()
            if "isolate" in b:
                s["isolate"] = bool(b["isolate"])
            if "auto_compare" in b:
                s["auto_compare"] = bool(b["auto_compare"])
            if b.get("country") in prices.COUNTRIES:
                s["country"] = b["country"]
            if "currency" in b and (b["currency"] in prices.CURRENCIES or b["currency"] == ""):
                s["currency"] = b["currency"]
            if "serper_key" in b:
                k = str(b["serper_key"]).strip()
                if not k.startswith("••••"):
                    s["serper_key"] = k[:120]
            write_json(SETTINGS, s)
        if s["isolate"]:
            cutout.prefetch()
        self.send(200, public_settings())

    # ---------------------------------------------------------------- PATCH
    def do_PATCH(self):
        self.guard(self._patch)

    def _patch(self):
        p, b = self.parts(), self.body()
        if len(p) == 3 and p[:2] == ["api", "groups"]:
            name = (b.get("name") or "").strip()[:60]
            if not name:
                raise Bad("Ponle un nombre.")
            with lock:
                g = load_groups()
                it = next((x for x in g if x["id"] == p[2]), None)
                if not it:
                    return self.send(404, {"error": "no encontrado"})
                it["name"] = name
                write_json(GROUPS, g)
            return self.send(200, it)
        if len(p) != 3 or p[:2] != ["api", "items"]:
            return self.send(404, {"error": "no encontrado"})
        iid = p[2]
        err = []

        def apply(it):
            if b.get("name"):
                it["name"] = str(b["name"])[:200]
            if "fav" in b:
                it["fav"] = bool(b["fav"])
            if "price" in b:
                pr = b["price"]
                if pr is None:
                    it["price"] = None
                else:
                    amt = prices.parse_price(pr.get("amount") if isinstance(pr, dict) else pr)
                    if not amt:
                        return err.append("Ese precio no es válido.")
                    cur = str((pr.get("currency") if isinstance(pr, dict) else "") or "").upper()[:3]
                    it["price"] = {"manual": True, "amount": amt, "currency": cur or prices.guess_currency(
                        it.get("url", ""), prices.COUNTRIES.get(get_settings()["country"], prices.COUNTRIES["CO"])["cur"])}
            if b.get("category") in CATEGORIES:
                it["category"] = b["category"]
            if "shop" in b:
                it["shop"] = bool(b["shop"])
                if not it["shop"]:
                    it["bought"] = False
            if "bought" in b:
                it["bought"] = bool(b["bought"])
            if isinstance(b.get("groups"), list):
                valid = {g["id"] for g in load_groups()}
                it["groups"] = [g for g in dict.fromkeys(b["groups"]) if g in valid]
            if b.get("add_image_data"):
                m = re.match(r"data:image/(png|jpeg|jpg|webp|gif);base64,(.+)$", b["add_image_data"], re.S)
                if not m:
                    return err.append("Esa imagen no es válida.")
                if len(it["images"]) >= MAX_IMGS:
                    return err.append(f"Máximo {MAX_IMGS} imágenes por idea.")
                it["images"].append(store(base64.b64decode(m.group(2)),
                                          "." + ("jpg" if m.group(1) == "jpeg" else m.group(1))))
            if b.get("add_image_url"):
                if len(it["images"]) >= MAX_IMGS:
                    return err.append(f"Máximo {MAX_IMGS} imágenes por idea.")
                fn = download_image(b["add_image_url"].strip(), it["url"])
                if not fn:
                    return err.append("No pude descargar esa imagen.")
                it["images"].append(fn)
                it["srcs"][fn] = b["add_image_url"].strip()
            rm = b.get("remove_image")
            if rm and rm in it["images"]:
                drop_image(it, rm)
            cv = b.get("cover")
            if cv and cv in it["images"]:
                it["images"].remove(cv)
                it["images"].insert(0, cv)

        it = update_item(iid, apply)
        if it is None:
            return self.send(404, {"error": "no encontrado"})
        if err:
            raise Bad(err[0])
        self.send(200, it)

    # ---------------------------------------------------------------- DELETE
    def do_DELETE(self):
        self.guard(self._delete)

    def _delete(self):
        p = self.parts()
        if len(p) == 3 and p[:2] == ["api", "groups"]:
            gid = p[2]
            with lock:
                write_json(GROUPS, [g for g in load_groups() if g["id"] != gid])
                items = load()
                for it in items:
                    if gid in it["groups"]:
                        it["groups"].remove(gid)
                save(items)
            return self.send(200, {"ok": True})
        if len(p) == 3 and p[:2] == ["api", "items"]:
            with lock:
                items = load()
                it = find(items, p[2])
                if it:
                    for fn in set(it["images"]) | set(it["alts"].values()) | set(it["alts"].keys()) | set(it["iso"]):
                        remove_image(fn)
                    save([i for i in items if i["id"] != p[2]])
            return self.send(200, {"ok": True})
        self.send(404, {"error": "no encontrado"})


def startup():
    with lock:  # trabajos que quedaron a medias al cerrar la ventana
        items = load()
        changed = False
        for it in items:
            if it["busy"]:
                it["busy"] = {}
                changed = True
        if changed:
            save(items)
        load_groups()
    if get_settings()["isolate"]:
        cutout.prefetch()


if __name__ == "__main__":
    startup()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    url = f"http://localhost:{PORT}"
    print(f"Mi Cuarto corriendo en {url}  (cierra esta ventana para detenerlo)")
    threading.Timer(1, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
