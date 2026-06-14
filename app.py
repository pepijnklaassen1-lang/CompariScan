import anthropic
import base64
import io
import os
import time
import threading
import requests
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from urllib.parse import quote
from collections import defaultdict
from PIL import Image, UnidentifiedImageError

API_KEY = os.environ.get("ANTHROPIC_API_KEY")
BOL_SITE_ID = os.environ.get("BOL_SITE_ID")  # optioneel: Bol.com partner site-id
BOL_CLIENT_ID = os.environ.get("BOL_CLIENT_ID")          # optioneel: Marketing Catalog API
BOL_CLIENT_SECRET = os.environ.get("BOL_CLIENT_SECRET")  # optioneel: Marketing Catalog API

client = anthropic.Anthropic(api_key=API_KEY)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 15 * 1024 * 1024  # max 15 MB upload
CORS(app)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "nl-NL,nl;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ----------------------------------------------------------------------
# Rate limiting: max 5 verzoeken per IP per minuut
# ----------------------------------------------------------------------
RATE_LIMIT = 5
RATE_WINDOW = 60  # seconden
ip_requests = defaultdict(list)
_laatste_opschoning = time.time()


def schoon_rate_limiter_op(nu):
    """Verwijder IP's zonder recente verzoeken, zodat het geheugen niet blijft groeien."""
    global _laatste_opschoning
    if nu - _laatste_opschoning < 300:  # max eens per 5 minuten
        return
    _laatste_opschoning = nu
    dode_ips = [ip for ip, tijden in ip_requests.items()
                if not tijden or nu - tijden[-1] > RATE_WINDOW]
    for ip in dode_ips:
        del ip_requests[ip]


def check_rate_limit(ip):
    nu = time.time()
    schoon_rate_limiter_op(nu)
    ip_requests[ip] = [t for t in ip_requests[ip] if nu - t < RATE_WINDOW]
    if len(ip_requests[ip]) >= RATE_LIMIT:
        wacht = int(RATE_WINDOW - (nu - ip_requests[ip][0])) + 1
        return False, wacht
    ip_requests[ip].append(nu)
    return True, 0


# ----------------------------------------------------------------------
# Afbeelding verwerken
# ----------------------------------------------------------------------
MAX_ZIJDE = 1568  # px — groter levert geen betere herkenning op, wel hogere kosten


def verwerk_afbeelding(data_bytes):
    """Valideer dat het bestand een afbeelding is en verklein/hercodeer naar JPEG.

    Retourneert (base64_string, media_type) of gooit ValueError.
    Door alles naar JPEG om te zetten blijven we altijd ruim onder de
    5 MB-limiet van de Anthropic API, ook bij grote telefoonfoto's.
    """
    try:
        img = Image.open(io.BytesIO(data_bytes))
        img.load()
    except (UnidentifiedImageError, OSError):
        raise ValueError("Het bestand is geen geldige afbeelding (JPG, PNG, GIF of WEBP).")

    # EXIF-rotatie van telefoonfoto's respecteren
    try:
        from PIL import ImageOps
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass

    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    if max(img.size) > MAX_ZIJDE:
        img.thumbnail((MAX_ZIJDE, MAX_ZIJDE))

    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=85)
    return base64.standard_b64encode(buffer.getvalue()).decode("utf-8"), "image/jpeg"


# ----------------------------------------------------------------------
# Productherkenning via Claude
# ----------------------------------------------------------------------
CATEGORIEEN = {"elektronica", "huishouden", "verzorging", "supermarkt",
               "speelgoed", "boeken", "kleding", "wonen", "sport", "overig"}


def herken_product(image_data, media_type):
    """Herken het product op de foto. Retourneert (productnaam, categorie) of (None, None)."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=60,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": image_data,
                    },
                },
                {
                    "type": "text",
                    "text": (
                        "Wat is dit voor product? Antwoord met exact twee regels.\n"
                        "Regel 1: alleen de merknaam en productnaam, zo kort en specifiek "
                        "mogelijk, maximaal 4 woorden. Bijvoorbeeld: 'Dobble' of "
                        "'LEGO Technic 42120' of 'Samsung QE55Q80C'.\n"
                        "Regel 2: exact één categorie uit deze lijst: elektronica, huishouden, "
                        "verzorging, supermarkt, speelgoed, boeken, kleding, wonen, sport, overig.\n"
                        "Als er geen duidelijk product op de foto staat, antwoord dan exact: ONBEKEND"
                    )
                }
            ],
        }]
    )
    tekst = response.content[0].text.strip()
    if tekst.upper().startswith("ONBEKEND"):
        return None, None
    regels = [r.strip() for r in tekst.split("\n") if r.strip()]
    naam = regels[0].split(".")[0].strip() if regels else ""
    categorie = regels[1].lower().strip() if len(regels) > 1 else "overig"
    if categorie not in CATEGORIEEN:
        categorie = "overig"
    if not naam or len(naam) > 80:
        return None, None
    return naam, categorie


# ----------------------------------------------------------------------
# Winkellinks (linkmodus)
#
# Live prijzen zijn nog niet beschikbaar: scraping wordt door de grote
# webshops geblokkeerd en is bovendien strijdig met hun voorwaarden.
# Tot de officiële bronnen beschikbaar zijn (bol API na affiliate-
# goedkeuring, daarna productfeeds van affiliatenetwerken) tonen we per
# winkel een directe link naar het product. Zodra er echte prijsdata is,
# vullen we het veld "prijs" en toont de frontend automatisch bedragen.
# ----------------------------------------------------------------------
def maak_bol_link(url):
    """Zet een Bol.com-link om naar een affiliate-link als BOL_SITE_ID is ingesteld."""
    if BOL_SITE_ID:
        return (f"https://partner.bol.com/click/click?p=1&t=url&s={BOL_SITE_ID}"
                f"&url={quote(url, safe='')}&f=TXL")
    return url


# ----------------------------------------------------------------------
# Bol Marketing Catalog API (officiele bron voor prijs, link en afbeelding)
#
# Werkt alleen als BOL_CLIENT_ID en BOL_CLIENT_SECRET zijn ingesteld;
# anders valt de site terug op de gewone linkmodus voor bol.
# Tokens zijn ongeveer 5 minuten geldig en moeten worden hergebruikt
# (eis van bol: niet per verzoek een nieuw token aanvragen).
# Resultaten worden kort gecachet; de affiliate-voorwaarden (art. 3.6)
# eisen dat getoonde prijzen overeenkomen met de actuele prijzen op bol,
# dus de cachetijd blijft bewust kort.
# ----------------------------------------------------------------------
BOL_CACHE_TIJD = 600  # seconden (10 minuten)
_bol_token = {"token": None, "verloopt": 0.0}
_bol_token_lock = threading.Lock()
_bol_cache = {}


def bol_token():
    """Geef een geldig Bearer-token, hergebruik tot vlak voor het verloopt."""
    with _bol_token_lock:
        nu = time.time()
        if _bol_token["token"] and nu < _bol_token["verloopt"] - 30:
            return _bol_token["token"]
        resp = requests.post(
            "https://login.bol.com/token?grant_type=client_credentials",
            auth=(BOL_CLIENT_ID, BOL_CLIENT_SECRET),
            headers={"Accept": "application/json"},
            timeout=6,
        )
        resp.raise_for_status()
        data = resp.json()
        _bol_token["token"] = data["access_token"]
        _bol_token["verloopt"] = nu + float(data.get("expires_in", 299))
        return _bol_token["token"]


def bol_zoek_product(zoekterm=None, ean=None):
    """Vraag het beste bol-aanbod op via de Marketing Catalog API.

    Retourneert {"titel", "prijs", "url", "afbeelding"} of None.
    Met een EAN wordt het product direct opgevraagd (13 cijfers, korter
    aanvullen met voorloopnullen volgens de GTIN-notatie); met alleen een
    zoekterm wordt het eerste zoekresultaat gebruikt.
    """
    if not (BOL_CLIENT_ID and BOL_CLIENT_SECRET):
        return None
    headers = {
        "Authorization": "Bearer " + bol_token(),
        "Accept": "application/json",
        "Accept-Language": "nl",
    }
    try:
        if ean:
            r = requests.get(
                f"https://api.bol.com/marketing/catalog/v1/products/{ean.zfill(13)}",
                params={"country-code": "NL", "include-image": "true", "include-offer": "true"},
                headers=headers, timeout=6)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            p = r.json()
        else:
            r = requests.get(
                "https://api.bol.com/marketing/catalog/v1/products/search",
                params={"search-term": zoekterm, "country-code": "NL", "page-size": 1,
                        "include-image": "true", "include-offer": "true"},
                headers=headers, timeout=6)
            r.raise_for_status()
            resultaten = r.json().get("results") or []
            if not resultaten:
                return None
            p = resultaten[0]
        aanbod = p.get("offer") or {}
        return {
            "titel": (p.get("title") or "").strip()[:80] or None,
            "prijs": aanbod.get("price"),
            "url": p.get("url"),
            "afbeelding": (p.get("image") or {}).get("url"),
        }
    except Exception as e:
        print(f"Bol API fout: {e}")
        return None


def bol_product_gecachet(zoekterm=None, ean=None):
    """Cache rond bol_zoek_product, zodat populaire scans de API-limiet sparen."""
    sleutel = ("ean", ean) if ean else ("zoek", (zoekterm or "").lower())
    nu = time.time()
    hit = _bol_cache.get(sleutel)
    if hit and nu - hit[0] < BOL_CACHE_TIJD:
        return hit[1]
    data = bol_zoek_product(zoekterm=zoekterm, ean=ean)
    if len(_bol_cache) > 500:  # simpele bescherming tegen onbeperkte groei
        _bol_cache.clear()
    _bol_cache[sleutel] = (nu, data)
    return data


# Winkels met hun zoek-URL en de categorieën die ze voeren.
# categorieen=None betekent: verkoopt vrijwel alles, altijd tonen.
# Let op: webshops wijzigen hun zoek-URL soms. Werkt een knop niet meer,
# pas dan alleen de "url" van die winkel hieronder aan (zoek handmatig op
# de site en kopieer de URL, vervang de zoekterm door {q}).
WINKELS = [
    {"naam": "Bol.com",      "url": "https://www.bol.com/nl/nl/s/?searchtext={q}",        "categorieen": None, "bol": True},
    {"naam": "Amazon.nl",    "url": "https://www.amazon.nl/s?k={q}",                      "categorieen": None},
    {"naam": "Coolblue",     "url": "https://www.coolblue.nl/zoeken?query={q}",           "categorieen": {"elektronica", "huishouden"}},
    {"naam": "MediaMarkt",   "url": "https://www.mediamarkt.nl/nl/search.html?query={q}", "categorieen": {"elektronica"}},
    {"naam": "Kruidvat",     "url": "https://www.kruidvat.nl/search?q={q}",               "categorieen": {"verzorging", "supermarkt"}},
    {"naam": "Etos",         "url": "https://www.etos.nl/search/?lang=nl_NL&q={q}",                  "categorieen": {"verzorging", "supermarkt"}},
    {"naam": "Trekpleister", "url": "https://www.trekpleister.nl/search?q={q}",           "categorieen": {"verzorging"}},
    {"naam": "HEMA",         "url": "https://www.hema.nl/search?q={q}&lang=nl_NL",                   "categorieen": {"huishouden", "wonen", "verzorging", "kleding", "speelgoed"}},
    {"naam": "fonQ",         "url": "https://www.fonq.nl/zoeken/?q={q}",                  "categorieen": {"wonen", "huishouden"}},
    {"naam": "Intertoys",    "url": "https://www.intertoys.nl/search?searchTerm={q}",     "categorieen": {"speelgoed"}},
    {"naam": "Bruna",        "url": "https://www.bruna.nl/zoeken/{q}"      ,                  "categorieen": {"boeken"}},
    {"naam": "Wehkamp",      "url": "https://www.wehkamp.nl/zoeken/?term={q}",            "categorieen": {"kleding", "wonen", "sport", "speelgoed"}},
    {"naam": "Decathlon",    "url": "https://www.decathlon.nl/search?Ntt={q}",            "categorieen": {"sport", "kleding"}},
]
MAX_WINKELS = 6  # maximum aantal winkels met een actieve linkknop


def haal_prijzen(zoekterm, categorie="overig", bol_data=None):
    """Bouw per winkel een resultaat. Relevante winkels komen bovenaan met een
    linkknop (prijs=None betekent: toon een linkknop). De overige winkels worden
    onderaan grijs meegegeven met relevant=False, zodat de bezoeker ziet dat ze
    wel zijn meegenomen maar dit type product niet voeren."""
    z = quote(zoekterm)
    relevant, niet_relevant = [], []
    for w in WINKELS:
        is_relevant = w["categorieen"] is None or categorie in w["categorieen"]
        if is_relevant and len(relevant) >= MAX_WINKELS:
            is_relevant = False  # lijst vol, toon de rest grijs
        if is_relevant:
            prijs, afbeelding = None, None
            if w.get("bol") and bol_data and bol_data.get("url"):
                # Officiele API-data: directe productlink, prijs en afbeelding
                link = maak_bol_link(bol_data["url"])
                prijs = bol_data.get("prijs")
                afbeelding = bol_data.get("afbeelding")
            else:
                link = w["url"].format(q=z)
                if w.get("bol"):
                    link = maak_bol_link(link)
            relevant.append({
                "winkel": w["naam"],
                "gevonden": True,
                "relevant": True,
                "prijs": prijs,
                "link": link,
                "afbeelding": afbeelding,
            })
        else:
            niet_relevant.append({
                "winkel": w["naam"],
                "gevonden": True,
                "relevant": False,
                "prijs": None,
                "link": None,
                "afbeelding": None,
            })
    return relevant + niet_relevant


# ----------------------------------------------------------------------
# Barcode (EAN) lookup
# ----------------------------------------------------------------------
def valideer_ean(code):
    """Controleer of de code een geldige EAN-8/EAN-13/UPC-A is (incl. controlecijfer)."""
    if not code or not code.isdigit() or len(code) not in (8, 12, 13):
        return False
    cijfers = [int(c) for c in code]
    controle = cijfers[-1]
    rest = cijfers[:-1][::-1]
    som = sum(d * 3 if i % 2 == 0 else d for i, d in enumerate(rest))
    return (10 - som % 10) % 10 == controle


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(".", "index.html")


# Eén route voor alle statische bestanden (contentpagina's, css, js,
# sitemap, robots, favicon). Nieuwe .html-pagina's in de map werken
# hierdoor automatisch, zonder aparte route per pagina.
# send_from_directory beschermt tegen paden buiten de map.
TOEGESTANE_EXTENSIES = (".html", ".css", ".js", ".xml", ".svg", ".png", ".ico")


@app.route("/<path:bestand>")
def statisch(bestand):
    ok = bestand == "robots.txt" or bestand.endswith(TOEGESTANE_EXTENSIES)
    if ok and "/" not in bestand:
        return send_from_directory(".", bestand)
    return send_from_directory(".", "404.html"), 404


@app.errorhandler(404)
def pagina_niet_gevonden(e):
    # Onbekende of ontbrekende paden krijgen de nette 404-pagina.
    return send_from_directory(".", "404.html"), 404


@app.errorhandler(413)
def bestand_te_groot(e):
    return jsonify({"fout": "De foto is te groot (maximaal 15 MB)."}), 413


@app.route("/herken", methods=["POST"])
def herken():
    ip = request.headers.get("X-Forwarded-For", request.remote_addr).split(",")[0].strip()
    toegestaan, wacht = check_rate_limit(ip)
    if not toegestaan:
        return jsonify({"fout": f"Te veel verzoeken. Probeer het over {wacht} seconden opnieuw."}), 429

    if "foto" not in request.files:
        return jsonify({"fout": "Geen foto ontvangen"}), 400
    bestand = request.files["foto"]
    if bestand.filename == "":
        return jsonify({"fout": "Leeg bestand"}), 400

    data_bytes = bestand.read()
    if len(data_bytes) == 0:
        return jsonify({"fout": "Bestand is leeg"}), 400

    try:
        image_data, media_type = verwerk_afbeelding(data_bytes)
    except ValueError as e:
        return jsonify({"fout": str(e)}), 400

    try:
        productnaam, categorie = herken_product(image_data, media_type)
    except anthropic.APIError as e:
        print(f"Anthropic API fout: {e}")
        return jsonify({"fout": "De productherkenning is tijdelijk niet beschikbaar. Probeer het zo opnieuw."}), 502

    if not productnaam:
        return jsonify({"fout": "Er is geen product herkend op deze foto. Probeer een duidelijkere foto van het product of de verpakking."}), 422

    bol_data = bol_product_gecachet(zoekterm=productnaam)
    return jsonify({
        "productnaam": productnaam,
        "afbeelding": bol_data.get("afbeelding") if bol_data else None,
        "prijzen": haal_prijzen(productnaam, categorie, bol_data)
    })


def zoek_naam_via_ean(ean):
    """Productnaam en afbeelding opzoeken via Open Food Facts / Open Products Facts.
    Retourneert (naam, categorie, afbeelding_url). Een treffer in Open Food Facts
    betekent vrijwel altijd een supermarkt- of drogisterijproduct."""
    bronnen = [
        (f"https://world.openfoodfacts.org/api/v2/product/{ean}.json", "supermarkt"),
        (f"https://world.openproductsfacts.org/api/v2/product/{ean}.json", "overig"),
    ]
    for url, categorie in bronnen:
        try:
            resp = requests.get(url, timeout=5, headers={"User-Agent": "CompariScan/1.0 (compariscan.nl)"})
            data = resp.json()
            product = data.get("product") or {}
            naam = (product.get("product_name") or "").strip()
            merk = (product.get("brands") or "").split(",")[0].strip()
            afbeelding = (product.get("image_front_url")
                          or product.get("image_url") or None)
            if isinstance(afbeelding, str) and not afbeelding.startswith("https://"):
                afbeelding = None  # alleen veilige https-afbeeldingen doorgeven
            if naam:
                volledig = f"{merk} {naam}".strip() if merk and merk.lower() not in naam.lower() else naam
                return volledig[:80], categorie, afbeelding
        except Exception as e:
            print(f"EAN-database fout ({url}): {e}")
    return None, None, None


@app.route("/barcode", methods=["POST"])
def barcode():
    ip = request.headers.get("X-Forwarded-For", request.remote_addr).split(",")[0].strip()
    toegestaan, wacht = check_rate_limit(ip)
    if not toegestaan:
        return jsonify({"fout": f"Te veel verzoeken. Probeer het over {wacht} seconden opnieuw."}), 429

    data = request.get_json(silent=True) or {}
    ean = str(data.get("ean", "")).strip()
    if not valideer_ean(ean):
        return jsonify({"fout": "Ongeldige barcode. Controleer de cijfers en probeer het opnieuw."}), 400

    naam, categorie, afbeelding = zoek_naam_via_ean(ean)
    bol_data = bol_product_gecachet(ean=ean)
    if not naam and bol_data and bol_data.get("titel"):
        naam = bol_data["titel"]
    if not afbeelding and bol_data:
        afbeelding = bol_data.get("afbeelding")
    zoekterm = naam if naam else ean
    productnaam = naam if naam else f"Barcode {ean}"

    return jsonify({"productnaam": productnaam,
                    "afbeelding": afbeelding,
                    "prijzen": haal_prijzen(zoekterm, categorie or "overig", bol_data)})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
