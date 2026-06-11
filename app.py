import anthropic
import base64
import io
import os
import time
import requests
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from urllib.parse import quote
from collections import defaultdict
from PIL import Image, UnidentifiedImageError

API_KEY = os.environ.get("ANTHROPIC_API_KEY")
BOL_SITE_ID = os.environ.get("BOL_SITE_ID")  # optioneel: Bol.com partner site-id

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
def herken_product(image_data, media_type):
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=50,
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
                        "Wat is dit voor product? Geef alleen de merknaam en productnaam, "
                        "zo kort en specifiek mogelijk. Maximaal 4 woorden. Geen beschrijving, "
                        "geen extra tekst. Bijvoorbeeld: 'Dobble' of 'LEGO Technic 42120' of "
                        "'Samsung QE55Q80C'. Als er geen duidelijk product op de foto staat, "
                        "antwoord dan exact: ONBEKEND"
                    )
                }
            ],
        }]
    )
    naam = response.content[0].text.strip()
    naam = naam.split("\n")[0].split(".")[0].strip()
    if not naam or naam.upper() == "ONBEKEND" or len(naam) > 80:
        return None
    return naam


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


def haal_prijzen(zoekterm):
    """Bouw per winkel een resultaat. prijs=None betekent: toon een linkknop."""
    z = quote(zoekterm)
    return [
        {
            "winkel": "Bol.com",
            "gevonden": True,
            "prijs": None,
            "link": maak_bol_link(f"https://www.bol.com/nl/nl/s/?searchtext={z}"),
            "afbeelding": None,
        },
        {
            "winkel": "Coolblue",
            "gevonden": True,
            "prijs": None,
            "link": f"https://www.coolblue.nl/zoeken?query={z}",
            "afbeelding": None,
        },
    ]


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


@app.route("/privacy.html")
def privacy():
    return send_from_directory(".", "privacy.html")


@app.route("/voorwaarden.html")
def voorwaarden():
    return send_from_directory(".", "voorwaarden.html")


@app.route("/style.css")
def stylesheet():
    return send_from_directory(".", "style.css")


@app.route("/app.js")
def scripts():
    return send_from_directory(".", "app.js")


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
        productnaam = herken_product(image_data, media_type)
    except anthropic.APIError as e:
        print(f"Anthropic API fout: {e}")
        return jsonify({"fout": "De productherkenning is tijdelijk niet beschikbaar. Probeer het zo opnieuw."}), 502

    if not productnaam:
        return jsonify({"fout": "Er is geen product herkend op deze foto. Probeer een duidelijkere foto van het product of de verpakking."}), 422

    prijzen = haal_prijzen(productnaam)

    return jsonify({
        "productnaam": productnaam,
        "prijzen": prijzen
    })


def zoek_naam_via_ean(ean):
    """Productnaam opzoeken via Open Food Facts / Open Products Facts.
    Gratis en legitiem; dekt vooral supermarkt- en drogisterijproducten."""
    bronnen = [
        f"https://world.openfoodfacts.org/api/v2/product/{ean}.json",
        f"https://world.openproductsfacts.org/api/v2/product/{ean}.json",
    ]
    for url in bronnen:
        try:
            resp = requests.get(url, timeout=5, headers={"User-Agent": "CompariScan/1.0 (compariscan.nl)"})
            data = resp.json()
            product = data.get("product") or {}
            naam = (product.get("product_name") or "").strip()
            merk = (product.get("brands") or "").split(",")[0].strip()
            if naam:
                volledig = f"{merk} {naam}".strip() if merk and merk.lower() not in naam.lower() else naam
                return volledig[:80]
        except Exception as e:
            print(f"EAN-database fout ({url}): {e}")
    return None


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

    naam = zoek_naam_via_ean(ean)
    zoekterm = naam if naam else ean
    productnaam = naam if naam else f"Barcode {ean}"

    return jsonify({"productnaam": productnaam, "prijzen": haal_prijzen(zoekterm)})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
