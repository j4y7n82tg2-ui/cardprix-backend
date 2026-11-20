"""
API CardPrix — point d'entrée.

Endpoints :
  GET /search?q=...                  -> recherche de cartes (API Pokemon TCG, fiable)
  GET /price?cardId=...&lang=FR      -> prix Cardmarket par état (TOUTES langues
                                         renvoyées, voir "cardmarket") + dernières
                                         ventes eBay biaisées vers `lang` (scraping,
                                         best-effort, voir scraper_*.py)
  GET /health                         -> vérification rapide que le serveur tourne

`lang` : FR (défaut) / EN / DE / IT / ES / JP — voir frontend/index.html (LANGUAGES).
Cardmarket est scrapé une seule fois par carte (toutes langues confondues,
plus efficace), donc changer de langue côté appli ne redemande PAS Cardmarket
tant que le cache est chaud ; seul eBay est re-demandé par langue.

Lancer en local :
  pip install -r requirements.txt
  playwright install chromium --with-deps
  uvicorn main:app --reload --port 8000

Voir README.md pour le déploiement (Render / Railway / Fly.io / VPS).
"""
import time
import logging
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pathlib import Path

import card_search
from scraper_cardmarket import get_cardmarket_prices, find_product_url, snapshot, debug_rows, CardmarketScrapeError
from scraper_ebay import get_ebay_sold, EbayScrapeError
from browser_pool import shutdown as shutdown_browser, uses_scraping_service

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("cardprix")

app = FastAPI(title="CardPrix API")

# En prod, remplace "*" par l'origine exacte de ton frontend une fois hébergé,
# pour ne pas laisser n'importe quel site appeler ton serveur.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# Cache mémoire simple : évite de re-scraper la même carte à chaque appel
# (plus rapide pour l'utilisateur, moins de sollicitation d'eBay/Cardmarket,
# donc aussi moins de risque de blocage anti-robot).
_cache: dict[str, tuple[float, dict]] = {}
CACHE_TTL_SECONDS = 6 * 3600


def _cache_get(key: str):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL_SECONDS:
        return hit[1]
    return None


def _cache_set(key: str, value: dict):
    _cache[key] = (time.time(), value)


@app.on_event("shutdown")
async def on_shutdown():
    await shutdown_browser()


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/search")
async def search(q: str = Query(..., min_length=2)):
    try:
        cards = await card_search.search_cards(q)
    except Exception as e:
        log.exception("search failed")
        raise HTTPException(502, f"Recherche de carte indisponible : {e}")
    return {"cards": cards}


SUPPORTED_LANGS = ["FR", "EN", "DE", "IT", "ES", "JP"]

# Coefficients indicatifs par état, relatifs à Near Mint (utilisés SEULEMENT en repli).
COND_FACTOR = {"MT": 1.10, "NM": 1.00, "EX": 0.85, "GD": 0.70, "LP": 0.60, "PL": 0.45, "PO": 0.30}


@app.get("/price")
async def price(cardId: str = Query(...), lang: str = Query("FR")):
    lang = lang.upper() if lang.upper() in SUPPORTED_LANGS else "FR"
    cache_key = f"{cardId}:{lang}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    card = await card_search.get_card(cardId)
    if not card:
        raise HTTPException(404, "Carte introuvable.")

    result = {"card": card, "cardmarket": None, "ebaySales": [], "warnings": [], "availableLanguages": []}

    # --- Cardmarket : prix par (langue, état), toutes langues en un seul scrape ---
    try:
        url = card.get("cardmarketUrl")
        if not url:
            # Aucun lien officiel connu (ex. série très récente) : on le retrouve par recherche.
            url = await find_product_url(card.get("name"), card.get("setName"), card.get("number"))
            card["cardmarketUrl"] = url
        result["cardmarket"] = await get_cardmarket_prices(url, lang=lang)
        result["availableLanguages"] = list(result["cardmarket"].keys())
    except CardmarketScrapeError as e:
        log.warning("cardmarket scrape failed for %s: %s", cardId, e)
        result["warnings"].append(
            f"Prix Cardmarket exact indisponible ({e}). Prix ESTIMÉ à partir du prix moyen "
            "Cardmarket, avec un coefficient par état — non spécifique à une langue."
        )
        if card.get("fallbackCardmarketPrice"):
            # Seul un prix moyen existe : on le décline par état avec des coefficients
            # indicatifs (le prix moyen Cardmarket correspond à peu près à Near Mint),
            # pour que le choix de l'état change quand même le résultat. ESTIMATION.
            p = float(card["fallbackCardmarketPrice"])
            flat = {k: [round(p * COND_FACTOR[k], 2)] for k in COND_FACTOR}
            result["cardmarket"] = {l: dict(flat) for l in SUPPORTED_LANGS}
            result["availableLanguages"] = SUPPORTED_LANGS
            result["cardmarketIsFallback"] = True

    # --- eBay : dernières ventes, biaisées vers la langue demandée ---
    try:
        query = f'pokemon {card["name"]} {card.get("setName", "")} {card.get("number", "")}'
        sales = await get_ebay_sold(query, limit=5, lang=lang)
        result["ebaySales"] = [
            {"date": s["date"] or "", "price": s["price"], "cond": s["title"][:60]}
            for s in sales
        ]
    except EbayScrapeError as e:
        log.warning("ebay scrape failed for %s (%s): %s", cardId, lang, e)
        result["warnings"].append(f"Ventes eBay indisponibles ({e}).")

    if result["cardmarket"] is None and not result["ebaySales"]:
        raise HTTPException(502, "Aucune source de prix disponible pour cette carte actuellement.")

    _cache_set(cache_key, result)
    return result


# --- Diagnostic : voir ce que le serveur voit réellement (pour régler le scraping) ---
_DEBUG_HOSTS = ("cardmarket.com", "ebay.fr", "ebay.com")


def _check_debug_url(url: str):
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    if not any(host == h or host.endswith("." + h) for h in _DEBUG_HOSTS):
        raise HTTPException(400, "Adresse non autorisée pour le diagnostic.")


@app.get("/debug/shot", include_in_schema=False)
async def debug_shot(url: str):
    """Capture d'écran de ce que voit le navigateur du serveur (ex. page anti-robot ?)."""
    _check_debug_url(url)
    try:
        return Response(await snapshot(url, png=True), media_type="image/png")
    except Exception as e:
        raise HTTPException(502, f"Capture impossible : {e}")


@app.get("/debug/rows", include_in_schema=False)
async def debug_rows_ep(url: str):
    """Chemin réel de récupération : statut, nombre de lignes, HTML de la 1re annonce, résultat parsé."""
    _check_debug_url(url)
    try:
        return await debug_rows(url)
    except Exception as e:
        raise HTTPException(502, f"Lecture impossible : {e}")


@app.get("/debug/config", include_in_schema=False)
async def debug_config():
    return {"scrapingService": uses_scraping_service()}


@app.get("/debug/info", include_in_schema=False)
async def debug_info(url: str):
    _check_debug_url(url)
    try:
        return await snapshot(url)
    except Exception as e:
        raise HTTPException(502, f"Lecture impossible : {e}")


@app.get("/debug/card", include_in_schema=False)
async def debug_card(id: str):
    """Réponse brute TCGdex + fiche normalisée + page Cardmarket trouvée (ou la raison de l'échec)."""
    out = {"raw": await card_search.raw_tcgdex_card(id), "card": await card_search.get_card(id)}
    try:
        c = out["card"] or {}
        out["cardmarketUrl"] = c.get("cardmarketUrl") or await find_product_url(
            c.get("name"), c.get("setName"), c.get("number"))
    except Exception as e:
        out["cardmarketError"] = str(e)
    return out


# --- Interface web : le serveur sert aussi l'appli (même adresse que l'API) ---
# Ouvre simplement l'adresse du serveur dans le navigateur du téléphone.
_INDEX = Path(__file__).parent / "static" / "index.html"


@app.get("/", include_in_schema=False)
async def index():
    if not _INDEX.exists():
        raise HTTPException(404, "Interface non trouvée (static/index.html manquant).")
    return FileResponse(_INDEX, media_type="text/html; charset=utf-8")
