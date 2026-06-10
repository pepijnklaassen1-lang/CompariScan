import anthropic
import base64
import os
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from urllib.parse import quote

API_KEY = os.environ.get("ANTHROPIC_API_KEY")

client = anthropic.Anthropic(api_key=API_KEY)

app = Flask(__name__)
CORS(app)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "nl-NL,nl;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def detecteer_media_type(data_bytes):
    if data_bytes[:4] == b'\x89PNG':
        return "image/png"
    elif data_bytes[:3] == b'GIF':
        return "image/gif"
    elif data_bytes[:4] == b'RIFF' and data_bytes[8:12] == b'WEBP':
        return "image/webp"
    else:
        return "image/jpeg"


def herken_product(image_data, media_type):
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
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
                    "text": "Wat is dit voor product? Geef alleen de merknaam en modelnaam, zo specifiek mogelijk. Bijvoorbeeld: 'Samsung QE55Q80C' of 'LEGO Technic 42120'."
                }
            ],
        }]
    )
    return response.content[0].text


def scrape_bol(productnaam):
    zoekterm = quote(productnaam)
    url = f"https://www.bol.com/nl/nl/s/?searchtext={zoekterm}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=8)
        soup = BeautifulSoup(resp.text, "html.parser")
        product = soup.select_one("[data-test='product-card']")
        if not product:
            return {"gevonden": False}
        naam_el = product.select_one("[data-test='product-title']")
        naam = naam_el.get_text(strip=True) if naam_el else productnaam
        prijs_el = product.select_one("[data-test='price-amount']")
        if not prijs_el:
            return {"gevonden": False}
        prijs_tekst = prijs_el.get_text(strip=True).replace(",", ".").replace("*", "")
        prijs = float(''.join(c for c in prijs_tekst if c.isdigit() or c == '.'))
        link_el = product.select_one("a[data-test='product-title']") or product.select_one("a")
        link = "https://www.bol.com" + link_el["href"] if link_el else url
        afbeelding_el = product.select_one("img")
        afbeelding = afbeelding_el["src"] if afbeelding_el and afbeelding_el.get("src") else None
        return {"gevonden": True, "naam": naam, "prijs": prijs, "link": link, "afbeelding": afbeelding}
    except Exception as e:
        print(f"Bol.com scrape fout: {e}")
        return {"gevonden": False}


def scrape_coolblue(productnaam):
    zoekterm = quote(productnaam)
    url = f"https://www.coolblue.nl/zoeken?query={zoekterm}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=8)
        soup = BeautifulSoup(resp.text, "html.parser")
        product = soup.select_one("[data-test='product-card']") or soup.select_one(".product-card")
        if not product:
            return {"gevonden": False}
        naam_el = product.select_one(".product-card__title") or product.select_one("a")
        naam = naam_el.get_text(strip=True) if naam_el else productnaam
        prijs_el = product.select_one("[data-test='price']") or product.select_one(".sales-price")
        if not prijs_el:
            return {"gevonden": False}
        prijs_tekst = prijs_el.get_text(strip=True).replace(",", ".").replace("€", "").strip()
        prijs = float(''.join(c for c in prijs_tekst if c.isdigit() or c == '.'))
        link_el = product.select_one("a")
        link = "https://www.coolblue.nl" + link_el["href"] if link_el and link_el.get("href", "").startswith("/") else url
        return {"gevonden": True, "naam": naam, "prijs": prijs, "link": link}
    except Exception as e:
        print(f"Coolblue scrape fout: {e}")
        return {"gevonden": False}


def haal_prijzen(productnaam):
    bol = scrape_bol(productnaam)
    coolblue = scrape_coolblue(productnaam)
    zoekterm = quote(productnaam)
    return [
        {
            "winkel": "Bol.com",
            "logo": "🔵",
            "gevonden": bol["gevonden"],
            "naam": bol.get("naam", productnaam),
            "prijs": bol.get("prijs"),
            "link": bol.get("link", f"https://www.bol.com/nl/nl/s/?searchtext={zoekterm}"),
            "afbeelding": bol.get("afbeelding"),
        },
        {
            "winkel": "Coolblue",
            "logo": "🔷",
            "gevonden": coolblue["gevonden"],
            "naam": coolblue.get("naam", productnaam),
            "prijs": coolblue.get("prijs"),
            "link": coolblue.get("link", f"https://www.coolblue.nl/zoeken?query={zoekterm}"),
            "afbeelding": None,
        },
    ]


@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/herken", methods=["POST"])
def herken():
    if "foto" not in request.files:
        return jsonify({"fout": "Geen foto ontvangen"}), 400
    bestand = request.files["foto"]
    if bestand.filename == "":
        return jsonify({"fout": "Leeg bestand"}), 400
    data_bytes = bestand.read()
    if len(data_bytes) == 0:
        return jsonify({"fout": "Bestand is leeg"}), 400
    media_type = detecteer_media_type(data_bytes)
    image_data = base64.standard_b64encode(data_bytes).decode("utf-8")
    productnaam = herken_product(image_data, media_type)
    prijzen = haal_prijzen(productnaam)
    return jsonify({
        "productnaam": productnaam,
        "prijzen": prijzen
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
