"""Comparador de precios: busca el producto en internet y dice dónde conviene comprarlo.

Fuentes (en este orden de confianza):
  1. Serper.dev / Google Shopping  (opcional, con una clave gratuita)
  2. MercadoLibre (API pública, si responde)
  3. Buscador web (DuckDuckGo) + lectura del precio de cada página de tienda
     (datos estructurados JSON-LD / meta / microdata que publican las propias tiendas)

Nunca inventa precios: si una tienda no publica un precio legible, no entra en la tabla.
"""
import html as htmlmod
import json
import os
import re
import unicodedata
import urllib.parse as up
import urllib.request as ur
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
DDG_URL = os.environ.get("MICUARTO_DDG", "https://html.duckduckgo.com/html/")
SERPER_URL = os.environ.get("MICUARTO_SERPER", "https://google.serper.dev/shopping")
ML_URL = os.environ.get("MICUARTO_ML", "https://api.mercadolibre.com")
RATES_URL = os.environ.get("MICUARTO_RATES", "https://open.er-api.com/v6/latest/USD")
RATES_FILE = None          # la define server.py (rates.json junto a los datos)
RATES_TTL = 12 * 3600      # se actualizan cada 12 horas
CURRENCIES = {"COP": "Peso colombiano (COP)", "USD": "Dólar estadounidense (USD)", "EUR": "Euro (EUR)",
              "MXN": "Peso mexicano (MXN)", "ARS": "Peso argentino (ARS)", "CLP": "Peso chileno (CLP)",
              "PEN": "Sol peruano (PEN)", "BRL": "Real brasileño (BRL)"}
BING_URL = os.environ.get("MICUARTO_BING", "https://www.bing.com/search")
LENS_URL = os.environ.get("MICUARTO_LENS", "https://google.serper.dev/lens")
_rates = {"at": 0, "rates": {}}

COUNTRIES = {
    "CO": {"name": "Colombia", "cur": "COP", "kl": "co-es", "gl": "co", "ml": "MCO", "mltld": "com.co"},
    "MX": {"name": "México", "cur": "MXN", "kl": "mx-es", "gl": "mx", "ml": "MLM", "mltld": "com.mx"},
    "AR": {"name": "Argentina", "cur": "ARS", "kl": "ar-es", "gl": "ar", "ml": "MLA", "mltld": "com.ar"},
    "CL": {"name": "Chile", "cur": "CLP", "kl": "cl-es", "gl": "cl", "ml": "MLC", "mltld": "cl"},
    "PE": {"name": "Perú", "cur": "PEN", "kl": "pe-es", "gl": "pe", "ml": "MPE", "mltld": "com.pe"},
    "ES": {"name": "España", "cur": "EUR", "kl": "es-es", "gl": "es", "ml": None, "mltld": None},
    "US": {"name": "Estados Unidos", "cur": "USD", "kl": "us-en", "gl": "us", "ml": None, "mltld": None},
}
TLD_CUR = {"co": "COP", "mx": "MXN", "ar": "ARS", "cl": "CLP", "pe": "PEN", "es": "EUR", "br": "BRL"}
SKIP_DOMAINS = ("pinterest.", "youtube.", "facebook.", "instagram.", "reddit.", "wikipedia.", "tiktok.",
                "twitter.", "x.com", "linkedin.", "quora.", "duckduckgo.", "google.", "bing.")
STORE_NAMES = {
    "mercadolibre": "MercadoLibre", "amazon": "Amazon", "falabella": "Falabella", "exito": "Éxito",
    "homecenter": "Homecenter", "alkosto": "Alkosto", "ktronix": "Ktronix", "panamericana": "Panamericana",
    "easy": "Easy", "ikea": "IKEA", "aliexpress": "AliExpress", "temu": "Temu", "shein": "Shein",
    "walmart": "Walmart", "ebay": "eBay", "linio": "Linio", "jumbo": "Jumbo", "olimpica": "Olímpica",
    "pepeganga": "Pepe Ganga", "tennis": "Tennis", "arturocalle": "Arturo Calle", "sodimac": "Sodimac",
    "cencosud": "Cencosud", "paris": "Paris", "ripley": "Ripley", "liverpool": "Liverpool", "etsy": "Etsy",
    "wayfair": "Wayfair", "target": "Target", "homedepot": "Home Depot", "bestbuy": "Best Buy",
}
STOP = set("de la el los las para con por una uno unos unas del al y o en the for and with a of to in on by "
           "un se su sus que es como mas más muy sin".split())


# ------------------------------------------------------------------ utilidades
def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn")


def tokens(s):
    t = re.findall(r"[a-z0-9]+", strip_accents(s).lower())
    return [x for x in t if x not in STOP and (len(x) >= 3 or x.isdigit())]


LOW = set("handmade hecho hecha mano artesanal custom personalizado personalizada decorativo decorativa decoracion "
          "decor nuevo nueva new original premium luxury lujo set pack kit regalo gift envio gratis oferta mini "
          "pequeno pequena grande vintage moderno moderna estilo style".split())


def match_score(query, text):
    """Qué tanto del producto buscado aparece en el texto. Cifras (medidas, modelos) y siglas pesan el doble."""
    q, t = tokens(query), set(tokens(text))
    if not q:
        return 0.0
    got = tot = 0.0
    for w in q:
        wt = 2.0 if any(ch.isdigit() for ch in w) else 0.4 if w in LOW else 1.0
        tot += wt
        if w in t or any((len(w) >= 4 and (x.startswith(w[:5]) or w.startswith(x[:5])) and len(x) >= 4) for x in t):
            got += wt
    return round(got / tot, 2)


def clean_query(name):
    s = htmlmod.unescape(name or "")
    s = re.sub(r"#\w+", " ", s)   # hashtags de Pinterest
    s = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", s)
    s = re.sub(r"(?i)^amazon\.[\w.]+\s*:\s*", "", s)
    parts = re.split(r"\s[|–—]\s|\s-\s|:\s", s)
    s = max(parts[:2], key=len) if len(parts) > 1 and len(parts[0]) < 12 else parts[0]
    s = re.sub(r"(?i)envío gratis|free shipping|oferta|official store|tienda oficial", " ", s)
    words = re.findall(r"[^\s,;]+", s)
    return " ".join(words[:10]).strip()


def parse_price(v):
    """'1.299.900' -> 1299900.0 ; '1,299.90' -> 1299.9 ; 129900 -> 129900.0 ; None si no es precio"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else None
    s = re.sub(r"[^\d.,]", "", str(v))
    if not s or not re.search(r"\d", s):
        return None
    if "," in s and "." in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        thou = "." if dec == "," else ","
        s = s.replace(thou, "").replace(dec, ".")
    elif "," in s or "." in s:
        sep = "," if "," in s else "."
        parts = s.split(sep)
        if len(parts) > 2 or (len(parts) == 2 and len(parts[1]) == 3 and len(parts[0]) <= 3 and parts[0] != "0"):
            s = "".join(parts)           # separador de miles
        else:
            s = ".".join(parts)          # decimales
    try:
        n = float(s)
    except ValueError:
        return None
    return n if n > 0 else None


def currency_of(text, default):
    t = (text or "").upper()
    if "US$" in t or "USD" in t:
        return "USD"
    if "€" in t or "EUR" in t:
        return "EUR"
    for c in ("COP", "MXN", "ARS", "CLP", "PEN", "BRL"):
        if c in t:
            return c
    return default


def domain_of(url):
    d = (up.urlparse(url).hostname or "").lower()
    return d[4:] if d.startswith("www.") else d


def store_of(url):
    d = domain_of(url)
    if re.fullmatch(r"[\d.]+", d):
        return d
    labels = d.split(".")
    core = labels[-2] if len(labels) >= 2 else d
    if len(labels) >= 3 and labels[-2] in ("com", "co", "net", "org"):
        core = labels[-3]
    return STORE_NAMES.get(core, core.capitalize())


def guess_currency(url, default):
    last = domain_of(url).split(".")[-1]
    return TLD_CUR.get(last, default)


def http_get(url, timeout=10, limit=2_500_000, headers=None, data=None, method=None):
    h = {"User-Agent": UA, "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
         "Accept-Language": "es-CO,es;q=0.9,en;q=0.8", "Accept-Encoding": "identity"}
    h.update(headers or {})
    req = ur.Request(url, headers=h, data=data, method=method)
    with ur.urlopen(req, timeout=timeout) as r:
        raw = r.read(limit)
        ct = r.headers.get("Content-Type", "")
        m = re.search(r"charset=([\w-]+)", ct, re.I) or re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", raw[:4000], re.I)
        enc = "utf-8"
        if m:
            g = m.group(1)
            enc = g.decode() if isinstance(g, bytes) else g
        try:
            return raw.decode(enc, errors="replace"), r.geturl()
        except LookupError:
            return raw.decode("utf-8", errors="replace"), r.geturl()


# ------------------------------------------------------------------ extracción
def _availability(v):
    s = str(v or "").lower()
    if any(k in s for k in ("outofstock", "soldout", "discontinued", "out_of_stock")):
        return "out"
    if any(k in s for k in ("instock", "onlineonly", "limitedavailability", "instoreonly", "in_stock")):
        return "in"
    return "unknown"


def _walk_products(node, out):
    if isinstance(node, list):
        for x in node:
            _walk_products(x, out)
    elif isinstance(node, dict):
        t = node.get("@type")
        types = t if isinstance(t, list) else [t]
        if any(isinstance(x, str) and x.lower() in ("product", "productgroup", "individualproduct") for x in types):
            out.append(node)
        for v in node.values():
            if isinstance(v, (dict, list)):
                _walk_products(v, out)


def _offers_of(product):
    offers = product.get("offers")
    if offers is None and product.get("hasVariant"):
        offers = [v.get("offers") for v in product["hasVariant"] if isinstance(v, dict) and v.get("offers")]
    flat = []

    def add(o):
        if isinstance(o, list):
            for x in o:
                add(x)
        elif isinstance(o, dict):
            if o.get("offers"):
                add(o["offers"])
            flat.append(o)

    add(offers)
    res = []
    for o in flat:
        spec = o.get("priceSpecification") if isinstance(o.get("priceSpecification"), dict) else {}
        raw = next((v for v in (o.get("price"), o.get("lowPrice"), spec.get("price")) if v not in (None, "")), None)
        price = parse_price(raw)
        if price:
            seller = o.get("seller")
            res.append({"price": price,
                        "currency": o.get("priceCurrency") or spec.get("priceCurrency"),
                        "availability": _availability(o.get("availability")),
                        "seller": seller.get("name") if isinstance(seller, dict) else (seller if isinstance(seller, str) else "")})
    return res


DEAD_RX = re.compile(r"page not found|p[aá]gina no encontrada|no se encontr[oó] la p[aá]gina|404 not found|"
                     r"this page (?:doesn't|does not) exist|esta p[aá]gina no existe|product (?:is )?no longer available|"
                     r"producto no (?:est[aá] )?disponible", re.I)


def looks_dead(html_text):
    """Páginas que responden 200 pero muestran 'no encontrada' (el precio que traen es de un producto que ya no está)."""
    body = re.sub(r"(?is)<(script|style|noscript|head)\b.*?</\1>", " ", html_text)
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))[:6000]
    return bool(DEAD_RX.search(text))


def extract_offers(html_text, url=""):
    """Precios que la página publica en datos estructurados. -> lista de ofertas (sin repetir)."""
    found, title = [], ""
    m = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.I | re.S)
    if m:
        title = re.sub(r"\s+", " ", htmlmod.unescape(m.group(1))).strip()
    og = re.search(r"<meta[^>]+(?:property|name)=[\"']og:title[\"'][^>]*content=[\"']([^\"']+)", html_text, re.I)
    if og:
        title = htmlmod.unescape(og.group(1)).strip()
    # 1) JSON-LD
    for block in re.findall(r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", html_text, re.I | re.S):
        try:
            data = json.loads(block.strip(), strict=False)
        except Exception:
            continue
        prods = []
        _walk_products(data, prods)
        for p in prods:
            for o in _offers_of(p):
                o["title"] = htmlmod.unescape(str(p.get("name") or title))[:200]
                found.append(o)
    # 2) meta de producto
    if not found:
        def meta(*keys):
            for k in keys:
                mm = re.search(r"<meta[^>]+(?:property|name|itemprop)=[\"']%s[\"'][^>]*content=[\"']([^\"']+)" % re.escape(k), html_text, re.I)
                if not mm:
                    mm = re.search(r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]*(?:property|name|itemprop)=[\"']%s[\"']" % re.escape(k), html_text, re.I)
                if mm:
                    return htmlmod.unescape(mm.group(1))
            return None
        price = parse_price(meta("product:price:amount", "og:price:amount", "twitter:data1", "price"))
        if price:
            found.append({"price": price, "currency": meta("product:price:currency", "og:price:currency", "priceCurrency"),
                          "availability": _availability(meta("product:availability", "og:availability")),
                          "seller": "", "title": title[:200]})
    # 3) microdata
    if not found:
        mm = re.search(r"itemprop=[\"']price[\"'][^>]*content=[\"']([^\"']+)", html_text, re.I)
        if mm and parse_price(mm.group(1)):
            cur = re.search(r"itemprop=[\"']priceCurrency[\"'][^>]*content=[\"']([^\"']+)", html_text, re.I)
            found.append({"price": parse_price(mm.group(1)), "currency": cur.group(1) if cur else None,
                          "availability": "unknown", "seller": "", "title": title[:200]})
    # 4) Amazon y tiendas que pintan el precio en el HTML. Solo bloques del precio principal
    #    (nunca el primer precio de la página: puede ser de un carrusel, un precio de lista o una variante).
    if not found and "a-offscreen" in html_text:
        raw = None
        for pat in (r"class=[\"'][^\"']*(?:priceToPay|apexPriceToPay)[^\"']*[\"'][^>]*>.{0,300}?class=[\"'][^\"']*a-offscreen[^\"']*[\"'][^>]*>([^<]{2,40})<",
                    r"id=[\"']priceblock_(?:ourprice|dealprice|saleprice)[\"'][^>]*>([^<]{2,40})<",
                    r"id=[\"']corePrice(?:Display)?_(?:desktop_)?feature_div[\"'].{0,1500}?class=[\"']a-price(?: aok-align-center)?[^\"']*[\"'][^>]*>.{0,300}?class=[\"']a-offscreen[\"'][^>]*>([^<]{2,40})<"):
            mm = re.search(pat, html_text, re.I | re.S)
            if mm:
                raw = htmlmod.unescape(mm.group(1)).replace("\xa0", " ")
                if parse_price(raw):
                    break
                raw = None
        pr = parse_price(raw) if raw else None
        if pr:
            found.append({"price": pr, "currency": currency_of(raw, None), "availability": "unknown",
                          "seller": "", "title": title[:200], "approx": True})
    # 5) Último recurso en páginas tipo Amazon: el precio que la propia página repite en sus datos internos.
    #    Solo si todas las apariciones coinciden (si hay varias versiones, no adivino).
    if not found and "a-offscreen" in html_text:
        vals = set(re.findall(r'"priceAmount"\s*:\s*"?([\d.]+)"?', html_text))
        if len(vals) == 1:
            pr = parse_price(next(iter(vals)))
            dp = re.search(r'"displayPrice"\s*:\s*"([^"]{1,30})"', html_text)
            if pr:
                found.append({"price": pr, "currency": currency_of(dp.group(1) if dp else "", None),
                              "availability": "unknown", "seller": "", "title": title[:200], "approx": True})
    seen, out = set(), []
    for o in found:
        k = (o["price"], o.get("currency"))
        if k not in seen:
            seen.add(k)
            out.append(o)
    return out


def parse_ddg(html_text):
    out = []
    for m in re.finditer(r"<a\b([^>]*)>(.*?)</a>", html_text, re.I | re.S):
        attrs = m.group(1)
        if "result__a" not in attrs:
            continue
        h = re.search(r"href=[\"']([^\"']+)", attrs)
        if not h:
            continue
        href = htmlmod.unescape(h.group(1))
        if href.startswith("//"):
            href = "https:" + href
        q = up.urlparse(href)
        if "duckduckgo.com" in q.netloc and q.path.startswith("/l/"):
            href = up.parse_qs(q.query).get("uddg", [""])[0]
        if href.startswith("http"):
            out.append((href, re.sub(r"\s+", " ", htmlmod.unescape(re.sub(r"<[^>]+>", "", m.group(2)))).strip()))
    return out


# ------------------------------------------------------------------ fuentes
def from_serper(query, country, key):
    c = COUNTRIES[country]
    body = json.dumps({"q": query, "gl": c["gl"], "hl": "es", "num": 20}).encode()
    txt, _ = http_get(SERPER_URL, timeout=15, data=body, method="POST",
                      headers={"X-API-KEY": key, "Content-Type": "application/json"})
    out = []
    for r in (json.loads(txt).get("shopping") or []):
        price = parse_price(r.get("price"))
        link = r.get("link") or ""
        if price and link.startswith("http"):
            out.append({"title": r.get("title") or "", "url": link, "store": r.get("source") or store_of(link),
                        "price": price, "currency": currency_of(r.get("price"), c["cur"]),
                        "availability": "unknown", "source": "Google Shopping"})
    return out


def from_mercadolibre(query, country):
    c = COUNTRIES[country]
    if not c["ml"]:
        return []
    txt, _ = http_get(f"{ML_URL}/sites/{c['ml']}/search?" + up.urlencode({"q": query, "limit": 12}), timeout=10,
                      headers={"Accept": "application/json"})
    out = []
    for r in (json.loads(txt).get("results") or []):
        price, link = parse_price(r.get("price")), r.get("permalink") or ""
        if price and link.startswith("http"):
            out.append({"title": r.get("title") or "", "url": link, "store": "MercadoLibre", "price": price,
                        "currency": r.get("currency_id") or c["cur"],
                        "availability": "out" if r.get("available_quantity") == 0 else "in",
                        "source": "MercadoLibre"})
    return out


def parse_bing(html_text):
    out = []
    for m in re.finditer(r'<li class="b_algo"[^>]*>.*?<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html_text, re.S):
        href = htmlmod.unescape(m.group(1))
        q = up.urlparse(href)
        if "bing.com" in q.netloc and q.path.startswith("/ck/"):
            u = up.parse_qs(q.query).get("u", [""])[0]
            if u.startswith("a1"):
                import base64
                try:
                    href = base64.urlsafe_b64decode(u[2:] + "=" * (-len(u[2:]) % 4)).decode("utf-8", "ignore")
                except Exception:
                    continue
        if href.startswith("http") and "bing.com" not in domain_of(href):
            out.append((href, re.sub(r"\s+", " ", htmlmod.unescape(re.sub(r"<[^>]+>", "", m.group(2)))).strip()))
    return out


def search_queries(query, country, extra=()):
    """Varias formas de preguntar lo mismo: así aparecen tiendas que una sola búsqueda no trae."""
    c = COUNTRIES[country]
    words = query.split()
    short = " ".join(words[:5])
    qs = [f"{query} comprar precio {c['name']}", f"{query} precio", f"{short} {c['name']}",
          f"{short} buy online price", f"\"{' '.join(words[:4])}\" shop"]
    for g in extra:
        if g:
            qs += [f"{g} comprar {c['name']}", f"{g} price"]
    seen, out = set(), []
    for q in qs:
        k = q.lower()
        if k not in seen and q.strip():
            seen.add(k)
            out.append(q)
    return out[:8]


def from_web(query, country, own_url, extra=()):
    """Busca en DuckDuckGo y Bing con varias formulaciones y lee el precio publicado en cada tienda."""
    c = COUNTRIES[country]
    queries = search_queries(query, country, extra)
    notes_ddg = []

    def search(args):
        kind, q = args
        try:
            if kind == "ddg":
                txt, _ = http_get(DDG_URL + "?" + up.urlencode({"q": q, "kl": c["kl"]}), timeout=12)
                res = parse_ddg(txt)
                if not res and re.search(r"anomaly|unusual traffic|captcha", txt[:20000], re.I):
                    notes_ddg.append("limit")
                return res
            txt, _ = http_get(BING_URL + "?" + up.urlencode({"q": q, "setlang": "es", "cc": c["gl"].upper()}), timeout=12)
            return parse_bing(txt)
        except Exception:
            return []

    jobs = [("ddg", q) for q in queries] + [("bing", q) for q in queries[:4]]
    with ThreadPoolExecutor(4) as ex:
        results = [r for lst in ex.map(search, jobs) for r in lst]
    if not results and notes_ddg:
        raise RuntimeError("El buscador limitó las consultas")
    # una página por tienda: la que mejor se parece al producto
    by_dom = {}
    for url, title in results:
        d = domain_of(url)
        if not d or any(s in d for s in SKIP_DOMAINS):
            continue
        sc = match_score(query, title + " " + " ".join(re.findall(r"[A-Za-z0-9]+", up.urlparse(url).path)))
        if d not in by_dom or sc > by_dom[d][0]:
            by_dom[d] = (sc, url, title)
    ranked = sorted(by_dom.values(), key=lambda x: -x[0])
    cands = [(u, t) for sc, u, t in ranked if sc >= 0.25][:24]

    def read(item):
        url, title = item
        try:
            page, final = http_get(url, timeout=9)
        except Exception:
            return []
        if looks_dead(page):
            return []
        res = []
        for o in extract_offers(page, final)[:2]:
            res.append({"title": o.get("title") or title, "url": final, "store": store_of(final),
                        "price": o["price"], "currency": o.get("currency") or guess_currency(final, c["cur"]),
                        "availability": o.get("availability", "unknown"), "source": "Página de la tienda"})
        return res

    with ThreadPoolExecutor(8) as ex:
        pages = list(ex.map(read, cands))
    return [o for lst in pages for o in lst], len(cands)


# ------------------------------------------------------------------ búsqueda por imagen
def image_search(image_url, country, key, target=None):
    """Busca la foto en internet (Google Lens vía Serper.dev). -> {results, guess}.
    Cada resultado: título, tienda, link y, si la página lo publica, precio."""
    c = COUNTRIES.get(country) or COUNTRIES["CO"]
    tgt = target if target in CURRENCIES else c["cur"]
    body = json.dumps({"url": image_url, "gl": c["gl"], "hl": "es"}).encode()
    txt, _ = http_get(LENS_URL, timeout=25, data=body, method="POST",
                      headers={"X-API-KEY": key, "Content-Type": "application/json"})
    data = json.loads(txt)
    rows = data.get("organic") or data.get("visual_matches") or data.get("results") or []
    cands, seen = [], set()
    for r in rows:
        link = r.get("link") or r.get("url") or ""
        title = r.get("title") or ""
        d = domain_of(link)
        if not link.startswith("http") or not d or any(s in d for s in SKIP_DOMAINS) or "google." in d or d in seen:
            continue
        seen.add(d)
        cands.append({"title": title, "url": link, "store": r.get("source") or store_of(link),
                      "thumb": r.get("thumbnailUrl") or r.get("imageUrl") or "", "price": None, "currency": None})
        if len(cands) >= 10:
            break

    def read(o):
        try:
            page, final = http_get(o["url"], timeout=9)
            offs = [] if looks_dead(page) else extract_offers(page, final)
            if offs:
                cur = (offs[0].get("currency") or guess_currency(final, c["cur"])).upper()
                o["price"], o["currency"] = offs[0]["price"], cur
                o["availability"] = offs[0].get("availability", "unknown")
        except Exception:
            pass
        return o

    with ThreadPoolExecutor(6) as ex:
        res = list(ex.map(read, cands))
    for o in res:
        if o["price"] and o["currency"] != tgt and o["currency"] in CURRENCIES:
            cv = convert(o["price"], o["currency"], tgt)
            if cv:
                o["orig"] = {"price": o["price"], "currency": o["currency"]}
                o["price"], o["currency"] = round(cv, 2), tgt
    res.sort(key=lambda o: (o["price"] is None, o["price"] or 0))
    # "Parece ser": el título que más comparte palabras con los demás resultados
    titles = [o["title"] for o in cands if o["title"]]
    freq = {}
    for t in titles:
        for w in set(tokens(t)):
            freq[w] = freq.get(w, 0) + 1
    guess = ""
    if titles:
        best = max(titles, key=lambda t: sum(freq[w] for w in set(tokens(t))) / (1 + len(set(tokens(t))) ** 0.5))
        guess = clean_query(best)
    return {"results": res, "guess": guess, "currency": tgt}


# ------------------------------------------------------------------ comparación
def quick_links(query, country):
    q = up.quote_plus(query)
    c = COUNTRIES[country]
    links = [("Google Shopping", f"https://www.google.com/search?tbm=shop&q={q}")]
    if c["mltld"]:
        links.append(("MercadoLibre", f"https://listado.mercadolibre.{c['mltld']}/{up.quote(query.replace(' ', '-'))}"))
    links.append(("Amazon", f"https://www.amazon.com/s?k={q}"))
    if country == "CO":
        links += [("Falabella", f"https://www.falabella.com.co/falabella-co/search?Ntt={q}"),
                  ("Éxito", f"https://www.exito.com/s?q={q}"),
                  ("Homecenter", f"https://www.homecenter.com.co/homecenter-co/search?Ntt={q}"),
                  ("Alkosto", f"https://www.alkosto.com/search?text={q}"),
                  ("IKEA", f"https://www.ikea.com/co/es/search/?q={q}")]
    links.append(("AliExpress", f"https://www.aliexpress.com/wholesale?SearchText={q}"))
    return [{"name": n, "url": u} for n, u in links]


def get_rates(force=False):
    """Tasas de cambio (base USD) desde internet, guardadas 12 h. {} si no hay forma de obtenerlas."""
    import time
    now = time.time()
    if not _rates["rates"] and RATES_FILE and os.path.exists(RATES_FILE):
        try:
            d = json.load(open(RATES_FILE, encoding="utf-8"))
            _rates.update(at=d.get("at", 0), rates=d.get("rates", {}))
        except Exception:
            pass
    if force or not _rates["rates"] or now - _rates["at"] > RATES_TTL:
        try:
            txt, _ = http_get(RATES_URL, timeout=8, headers={"Accept": "application/json"})
            r = json.loads(txt).get("rates") or {}
            r = {k: float(v) for k, v in r.items() if k in CURRENCIES and isinstance(v, (int, float)) and v > 0}
            if len(r) >= 3:
                r["USD"] = 1.0
                _rates.update(at=now, rates=r)
                if RATES_FILE:
                    try:
                        json.dump({"at": now, "rates": r}, open(RATES_FILE, "w", encoding="utf-8"))
                    except Exception:
                        pass
        except Exception:
            pass  # sin internet: se usan las últimas tasas guardadas, si las hay
    return dict(_rates["rates"]), _rates["at"]


def convert(amount, frm, to):
    """amount en 'frm' -> en 'to'. None si no hay tasa."""
    if frm == to:
        return amount
    r, _ = get_rates()
    if frm in r and to in r:
        return amount / r[frm] * r[to]
    return None


def money(v, cur):
    n = f"{v:,.0f}".replace(",", ".") if v >= 1000 or float(v).is_integer() else f"{v:,.2f}"
    return f"{n} {cur}"


def compare(name, own_url="", own_price=None, settings=None, query=None, extra=None):
    """-> dict con ofertas ordenadas, la mejor opción, un resumen y avisos."""
    settings = settings or {}
    country = settings.get("country") if settings.get("country") in COUNTRIES else "CO"
    c = COUNTRIES[country]
    tgt = settings.get("currency") if settings.get("currency") in CURRENCIES else c["cur"]  # moneda en la que se compara
    query_given = bool(query and query.strip())
    query = (query or clean_query(name)).strip()
    notes, raw = [], []
    key = (settings.get("serper_key") or "").strip()

    raw += list((extra or {}).get("offers") or [])           # lo que encontró la búsqueda por foto
    guesses = [(extra or {}).get("guess")] if (extra or {}).get("guess") and not query_given else []

    def job_serper():
        if not key:
            return []
        try:
            return from_serper(query, country, key) + sum([from_serper(g, country, key) for g in guesses], [])
        except Exception as e:
            code = getattr(e, "code", None)
            why = ("la clave no es válida" if code in (401, 403) else "se acabó el límite de tu plan" if code == 429
                   else "error " + (str(code) if code else e.__class__.__name__))
            notes.append(f"Serper.dev no respondió ({why}). Revisa tu clave en Configuración.")
            return []

    def job_ml():
        try:
            return from_mercadolibre(query, country)
        except Exception:
            return []   # la API pública de MercadoLibre puede pedir sesión: se ignora sin ruido

    def job_web():
        try:
            web, n = from_web(query, country, own_url, guesses)
            if n == 0:
                notes.append("El buscador no devolvió tiendas para esta búsqueda.")
            return web
        except Exception as e:
            notes.append("No pude consultar el buscador web (" + (str(e) or e.__class__.__name__) +
                         "). Para resultados más estables agrega una clave gratuita de Serper.dev en Configuración.")
            return []

    with ThreadPoolExecutor(3) as ex:
        futs = [ex.submit(f) for f in (job_serper, job_ml, job_web)]
        for f in futs:
            raw += f.result()

    if own_price:
        raw.append({"title": name, "url": own_url, "store": store_of(own_url) if own_url else "Tu link",
                    "price": own_price.get("amount"), "currency": own_price.get("currency") or guess_currency(own_url, c["cur"]),
                    "availability": "unknown", "source": "Tu link guardado", "you": True,
                    "approx": bool(own_price.get("approx"))})

    MIN_SHOW, HIGH = 0.6, 0.65      # por debajo de MIN_SHOW no se muestra; "alta" = HIGH o más
    offers, dropped, seen, converted = [], 0, {}, 0
    own_key = re.sub(r"[?#].*$", "", own_url or "").rstrip("/").lower()
    full = htmlmod.unescape(name or "")
    score_q = clean_query(full) if not query_given else query
    for o in raw:
        if not o.get("price") or not o.get("url"):
            continue
        o["currency"] = (o.get("currency") or c["cur"]).upper()
        if o["currency"] != tgt and o["currency"] in CURRENCIES:
            cv = convert(o["price"], o["currency"], tgt)
            if cv:
                o["orig"] = {"price": o["price"], "currency": o["currency"]}
                o["price"], o["currency"] = round(cv, 2), tgt
                converted += 1
        if not o.get("you") and own_key and re.sub(r"[?#].*$", "", o["url"]).rstrip("/").lower() == own_key:
            continue  # es el mismo listado de tu link
        slug = " ".join(re.findall(r"[A-Za-z0-9]+", up.urlparse(o["url"]).path))
        o["match"] = 1.0 if o.get("you") else match_score(score_q, (o.get("title") or "") + " " + slug)
        if o["match"] < MIN_SHOW:
            dropped += 1
            continue
        o["domain"] = domain_of(o["url"])
        k = (o["domain"], o["currency"], bool(o.get("you")))
        if k in seen:  # una tienda: se queda la oferta más barata
            if o["price"] < seen[k]["price"]:
                seen[k].update(o)
            continue
        seen[k] = o
        offers.append(o)
    if converted:
        notes.append(f"{converted} precio(s) estaban en otra moneda y los convertí a {tgt} con la tasa de hoy (aproximado: no incluye envío ni impuestos).")
    if dropped:
        notes.append(f"Descarté {dropped} resultado(s) que no parecían ser este mismo producto.")

    # Precio de referencia: el de tu link; si no hay, la mediana de las coincidencias altas.
    same = [o for o in offers if o["currency"] == tgt]
    mine = next((o for o in same if o.get("you")), None)
    highs = sorted(o["price"] for o in same if o["match"] >= HIGH and not o.get("you"))
    anchor = mine["price"] if mine else (highs[len(highs) // 2] if len(highs) >= 3 else None)
    for o in offers:
        o["odd"] = bool(anchor and not o.get("you") and o["currency"] == tgt
                        and (o["price"] < anchor * 0.45 or o["price"] > anchor * 2.2))

    def rank(o):
        return (0 if o["currency"] == tgt else 1, 1 if o["availability"] == "out" else 0, o["price"])

    offers.sort(key=rank)
    pool = [o for o in offers if o["currency"] == tgt and o["availability"] != "out"]
    sure = [o for o in pool if (o.get("you") or o["match"] >= HIGH) and not o["odd"]]
    maybe = [o for o in pool if not o["odd"]]
    best = min(sure, key=lambda o: o["price"]) if sure else None   # sin coincidencia clara no recomiendo nada
    low_conf = bool(best and not best.get("you") and best["match"] < HIGH)
    if any(o["odd"] for o in offers):
        notes.append("Marqué como «atípico» un precio muy distinto al de referencia: suele ser otra versión, un accesorio o un paquete. No cuenta como mejor opción.")
    if not mine:
        if "amazon" in (own_url or "").lower():
            notes.append("Amazon no me dejó leer el precio de tu link (bloquea la lectura automática o muestra otro precio sin tu sesión). Escríbelo abajo, o guarda la idea con el botón «Guardar en Idearium» desde la página del producto: ese lo lee desde tu navegador.")
        else:
            notes.append("No pude leer el precio de tu link, así que no lo comparé. Escríbelo abajo (o pega el que ves en la tienda) y recalculo.")

    summary = ""
    if best:
        summary = f"Mejor opción: {best['store']} a {money(best['price'], best['currency'])}"
        if best.get("you"):
            summary += ". Tu link ya tiene el mejor precio que encontré"
        elif mine and mine["price"] > best["price"]:
            diff = mine["price"] - best["price"]
            summary += f". Ahorras {money(diff, best['currency'])} ({round(diff * 100 / mine['price'])}%) frente a tu link"
        summary += "."
        if low_conf:
            notes.append("La coincidencia con el producto no es alta: confirma que sea el mismo antes de comprar.")
    elif maybe:
        summary = "No encontré una tienda que coincida claramente con este producto. Abajo dejo las más parecidas por si alguna te sirve."
    elif offers:
        summary = f"Encontré precios, pero ninguno en {tgt}. Revísalos abajo."
    else:
        summary = "No encontré precios verificables para este producto."
    other = [o for o in offers if o["currency"] != tgt]
    if other and best:
        notes.append(f"{len(other)} oferta(s) están en otra moneda y no se comparan con {tgt}.")

    return {"query": query, "country": country, "currency": tgt,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "offers": offers[:15], "best": best, "own_missing": not mine, "own_unverified": bool(mine and mine.get("approx")), "summary": summary, "notes": notes,
            "links": quick_links(query, country)}
