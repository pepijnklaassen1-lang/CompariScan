// ==================== Tabs ====================
function wisselTab(naam) {
    if (naam !== "barcode") stopScanner();
    ["barcode", "foto"].forEach(t => {
        document.getElementById("tab-" + t).classList.toggle("actief", t === naam);
        document.getElementById("tab-" + t).setAttribute("aria-selected", String(t === naam));
        document.getElementById("paneel-" + t).classList.toggle("actief", t === naam);
    });
    verbergFout();
}

// ==================== Barcode scanner ====================
let stream = null, scanActief = false, zxingReader = null, laatsteCode = null;

function zetStatus(t) { document.getElementById("scanner-status").textContent = t; }

async function nativeScannerBeschikbaar() {
    // Let op: Chrome op Windows/Linux definieert BarcodeDetector wél,
    // maar ondersteunt vaak geen enkel formaat. Daarom expliciet controleren.
    if (!("BarcodeDetector" in window)) return false;
    try {
        const formaten = await BarcodeDetector.getSupportedFormats();
        return formaten.includes("ean_13");
    } catch (e) { return false; }
}

async function startScanner() {
    verbergFout();
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        toonFout("Je browser ondersteunt geen camera. Typ de barcode hieronder in, of gebruik de fototab.");
        return;
    }
    const video = document.getElementById("scanner-video");
    document.getElementById("scan-start").style.display = "none";
    document.getElementById("scan-stop").style.display = "block";
    scanActief = true;
    laatsteCode = null;

    if (await nativeScannerBeschikbaar()) {
        await startNatief(video);
    } else {
        await startZxing(video);
    }
}

async function startNatief(video) {
    try {
        stream = await navigator.mediaDevices.getUserMedia({
            video: { facingMode: "environment", width: { ideal: 1280 } }, audio: false
        });
    } catch (e) {
        cameraGeweigerd(); return;
    }
    video.srcObject = stream;
    await video.play();
    document.getElementById("scanner-kader").style.display = "block";
    zetStatus("Richt de camera op de streepjescode...");

    const detector = new BarcodeDetector({ formats: ["ean_13", "ean_8", "upc_a"] });
    let fouten = 0;
    const loop = async () => {
        if (!scanActief) return;
        try {
            const codes = await detector.detect(video);
            if (codes.length > 0 && verwerkDetectie(codes[0].rawValue)) return;
        } catch (e) {
            // Detector blijkt toch niet te werken: overschakelen naar ZXing
            if (++fouten >= 5) {
                if (stream) { stream.getTracks().forEach(t => t.stop()); stream = null; }
                startZxing(video);
                return;
            }
        }
        setTimeout(loop, 150);
    };
    loop();
}

function laadZxing() {
    return new Promise((res, rej) => {
        if (window.ZXing) return res();
        const bronnen = [
            "https://cdn.jsdelivr.net/npm/@zxing/library@0.21.3/umd/index.min.js",
            "https://unpkg.com/@zxing/library@0.21.3/umd/index.min.js"
        ];
        const probeer = (i) => {
            if (i >= bronnen.length) return rej(new Error("CDN niet bereikbaar"));
            const s = document.createElement("script");
            s.src = bronnen[i];
            s.onload = () => window.ZXing ? res() : probeer(i + 1);
            s.onerror = () => probeer(i + 1);
            document.head.appendChild(s);
        };
        probeer(0);
    });
}

async function startZxing(video) {
    zetStatus("Scanner wordt geladen...");
    try { await laadZxing(); } catch (e) {
        toonFout("De scanner kon niet worden geladen. Typ de barcode handmatig in.");
        stopScanner(""); return;
    }
    const hints = new Map();
    hints.set(ZXing.DecodeHintType.POSSIBLE_FORMATS,
        [ZXing.BarcodeFormat.EAN_13, ZXing.BarcodeFormat.EAN_8, ZXing.BarcodeFormat.UPC_A]);
    zxingReader = new ZXing.BrowserMultiFormatReader(hints);
    zxingReader.timeBetweenDecodingAttempts = 150;

    const callback = (result) => {
        if (result && scanActief) verwerkDetectie(result.getText());
    };
    try {
        // ZXing beheert hier zelf de camerastream
        if (typeof zxingReader.decodeFromConstraints === "function") {
            await zxingReader.decodeFromConstraints(
                { video: { facingMode: "environment", width: { ideal: 1280 } }, audio: false },
                video, callback);
        } else {
            await zxingReader.decodeFromVideoDevice(undefined, video, callback);
        }
        document.getElementById("scanner-kader").style.display = "block";
        zetStatus("Richt de camera op de streepjescode...");
    } catch (e) {
        console.error("ZXing camera fout:", e);
        cameraGeweigerd();
    }
}

function cameraGeweigerd() {
    toonFout("Geen toegang tot de camera. Geef toestemming in je browser, of typ de barcode handmatig in.");
    stopScanner("");
}

// Twee identieke detecties op rij vereist: voorkomt misleesfouten
function verwerkDetectie(code) {
    if (!scanActief) return true;
    if (code !== laatsteCode) { laatsteCode = code; return false; }
    scanActief = false;
    if (navigator.vibrate) navigator.vibrate(100);
    stopScanner("Barcode herkend: " + code);
    zoekViaEan(code);
    return true;
}

function stopScanner(statusTekst) {
    scanActief = false;
    if (zxingReader) { try { zxingReader.reset(); } catch (e) {} zxingReader = null; }
    if (stream) { stream.getTracks().forEach(t => t.stop()); stream = null; }
    const video = document.getElementById("scanner-video");
    if (video) video.srcObject = null;
    document.getElementById("scanner-kader").style.display = "none";
    document.getElementById("scan-start").style.display = "block";
    document.getElementById("scan-stop").style.display = "none";
    if (statusTekst) zetStatus(statusTekst);
}

// ==================== Zoeken via EAN ====================
function geldigEan(code) {
    if (!/^\d{8}$|^\d{12}$|^\d{13}$/.test(code)) return false;
    const cijfers = code.split("").map(Number);
    const controle = cijfers.pop();
    const som = cijfers.reverse().reduce((s, d, i) => s + d * (i % 2 === 0 ? 3 : 1), 0);
    return (10 - (som % 10)) % 10 === controle;
}

async function zoekViaEan(code) {
    code = (code || "").replace(/\s/g, "");
    verbergFout();
    if (!geldigEan(code)) {
        toonFout("Dat lijkt geen geldige barcode. Controleer de cijfers (8 of 13 cijfers).");
        return;
    }
    document.getElementById("ean-invoer").value = code;
    toonLaden("Product en prijzen worden opgezocht voor barcode " + code + "...");
    try {
        const response = await fetch("/barcode", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ean: code })
        });
        let data = null;
        try { data = await response.json(); } catch (e) {}
        if (!response.ok) throw new Error((data && data.fout) ? data.fout : "Serverfout (" + response.status + ").");
        toonResultaat(data, null);
    } catch (err) {
        toonFout(err.message);
    } finally {
        verbergLaden();
    }
}

// ==================== Foto-herkenning (start direct na kiezen) ====================
document.getElementById("foto").addEventListener("change", function () {
    const foto = this.files[0];
    if (!foto) return;
    const preview = document.getElementById("foto-preview");
    preview.src = URL.createObjectURL(foto);
    preview.style.display = "block";
    document.getElementById("upload-label").textContent = foto.name;
    verbergFout();
    herken(foto); // direct zoeken, geen extra klik
});

async function comprimeer(foto) {
    if (foto.size < 1.5 * 1024 * 1024) return foto;
    try {
        const bitmap = await createImageBitmap(foto);
        const maxZijde = 1600;
        const schaal = Math.min(1, maxZijde / Math.max(bitmap.width, bitmap.height));
        const canvas = document.createElement("canvas");
        canvas.width = Math.round(bitmap.width * schaal);
        canvas.height = Math.round(bitmap.height * schaal);
        canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
        const blob = await new Promise(res => canvas.toBlob(res, "image/jpeg", 0.85));
        return blob || foto;
    } catch (e) { return foto; }
}

async function herken(foto) {
    toonLaden("Product wordt herkend en prijzen worden opgezocht... (kan 15 seconden duren)");
    try {
        const verkleind = await comprimeer(foto);
        const formData = new FormData();
        formData.append("foto", verkleind, "foto.jpg");
        const response = await fetch("/herken", { method: "POST", body: formData });
        let data = null;
        try { data = await response.json(); } catch (e) {}
        if (!response.ok) throw new Error((data && data.fout) ? data.fout : "Serverfout (" + response.status + ").");
        toonResultaat(data, foto);
    } catch (err) {
        toonFout(err.message);
    } finally {
        verbergLaden();
    }
}

// ==================== Gedeelde weergave ====================
function toonLaden(tekst) {
    document.getElementById("laadtekst").textContent = tekst;
    document.getElementById("laadindicator").style.display = "block";
    document.getElementById("resultaat").style.display = "none";
    verbergFout();
}
function verbergLaden() {
    document.getElementById("laadindicator").style.display = "none";
}
function toonFout(tekst) {
    const fout = document.getElementById("fout");
    fout.textContent = tekst;
    fout.style.display = "block";
}
function verbergFout() { document.getElementById("fout").style.display = "none"; }

function maakWinkelKaart(winkel, isGoedkoopst) {
    const kaart = document.createElement("div");

    if (!winkel.gevonden) {
        kaart.className = "winkel-kaart niet-gevonden";
        const naam = document.createElement("div");
        naam.className = "winkel-naam";
        naam.textContent = winkel.winkel;
        kaart.appendChild(naam);
        const ng = document.createElement("div");
        ng.className = "niet-gevonden-tekst";
        ng.textContent = "Niet gevonden";
        kaart.appendChild(ng);
        return kaart;
    }

    kaart.className = "winkel-kaart" + (isGoedkoopst ? " goedkoopst" : "");
    const tekstBlok = document.createElement("div");
    const naam = document.createElement("div");
    naam.className = "winkel-naam";
    naam.textContent = winkel.winkel + " ";
    if (isGoedkoopst) {
        const label = document.createElement("span");
        label.className = "goedkoopst-label";
        label.textContent = "Goedkoopst";
        naam.appendChild(label);
    }
    tekstBlok.appendChild(naam);
    const link = document.createElement("a");
    link.className = "winkel-link";
    link.href = winkel.link;
    link.target = "_blank";
    link.rel = "noopener noreferrer sponsored";
    link.textContent = "Bekijk aanbieding";
    tekstBlok.appendChild(link);
    kaart.appendChild(tekstBlok);

    const prijs = document.createElement("div");
    prijs.className = "winkel-prijs";
    prijs.textContent = "€" + winkel.prijs.toFixed(2).replace(".", ",");
    kaart.appendChild(prijs);
    return kaart;
}

function toonResultaat(data, fotoFallback) {
    const metAfbeelding = data.prijzen.find(w => w.afbeelding);
    const productfoto = document.getElementById("productfoto");
    if (metAfbeelding) productfoto.src = metAfbeelding.afbeelding;
    else if (fotoFallback) productfoto.src = URL.createObjectURL(fotoFallback);
    else productfoto.removeAttribute("src");
    document.getElementById("productnaam").textContent = data.productnaam;

    const gevondenPrijzen = data.prijzen.filter(w => w.gevonden).map(w => w.prijs);
    const goedkoopste = gevondenPrijzen.length > 0 ? Math.min(...gevondenPrijzen) : null;
    const lijst = document.getElementById("winkel-lijst");
    lijst.innerHTML = "";
    data.prijzen.forEach(winkel => {
        lijst.appendChild(maakWinkelKaart(winkel, winkel.gevonden && winkel.prijs === goedkoopste));
    });
    document.getElementById("resultaat").style.display = "block";
}
