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
import asyncio
import logging
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pathlib import Path

import card_search
import cache as store
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

# Cache (voir cache.py) : les données d'une carte sont gardées 24 h ; passé ce délai,
# l'ancienne valeur est renvoyée INSTANTANÉMENT pendant qu'une mise à jour se fait
# en arrière-plan (jusqu'à 7 jours), au lieu de faire attendre l'utilisateur.
PRICE_FRESH = 24 * 3600
PRICE_STALE_MAX = 7 * 24 * 3600
DEGRADED_FRESH = 600            # un résultat "estimation" n'est gardé que 10 min
CARD_TTL = 24 * 3600            # contient le prix Cardmarket agrégé du jour
FAIL_TTL = 6 * 3600             # un échec de lecture détaillée n'est pas retenté avant 6 h (sauf « Réessayer »)
CMURL_TTL = 30 * 24 * 3600      # le lien Cardmarket d'une carte ne change pas
_inflight: dict[str, asyncio.Task] = {}


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


PRICE_DEADLINE_SECONDS = 85   # Render coupe les requêtes trop longues (~100 s)


async def _compute_price(cardId: str, lang: str, force: bool = False) -> tuple[dict, bool]:
    """Lit les sources (en parallèle) et renvoie (résultat, dégradé?). Lève HTTPException si rien."""
    card = await _get_card_cached(cardId)
    result = {"card": card, "mode": "precise", "cardmarket": None, "ebaySales": [], "warnings": [], "availableLanguages": []}

    async def cardmarket_job():
        """Prix Cardmarket par (langue, état) ; en cas d'échec -> estimation à partir du prix moyen."""
        try:
            failed = None if force else await store.get(f"cmfail:{cardId}:{lang}")
            if failed:
                raise CardmarketScrapeError(f"échec récent, pas de nouvel essai avant quelques heures : {failed}")
            url = card.get("cardmarketUrl") or await store.get(f"cmurl:{cardId}")
            if not url:
                # Aucun lien connu (ex. série très récente) : on le retrouve par recherche, une seule fois.
                url = await find_product_url(card.get("name"), card.get("setName"), card.get("number"))
                await store.set(f"cmurl:{cardId}", url, CMURL_TTL)
            card["cardmarketUrl"] = url
            result["cardmarket"] = await get_cardmarket_prices(url, lang=lang)
            result["availableLanguages"] = list(result["cardmarket"].keys())
        except Exception as e:   # CardmarketScrapeError, délai réseau, etc. : jamais de plantage
            log.warning("cardmarket failed for %s: %s", cardId, e)
            reason = str(e) or type(e).__name__
            if "échec récent" not in reason:
                await store.set(f"cmfail:{cardId}:{lang}", reason[:300], FAIL_TTL)
            _cardmarket_fallback(result, card, reason)

    async def ebay_job():
        try:
            if not force and await store.get(f"ebfail:{cardId}:{lang}"):
                return
            query = f'pokemon {card["name"]} {card.get("setName", "")} {card.get("number", "")}'
            sales = await get_ebay_sold(query, limit=5, lang=lang)
            result["ebaySales"] = [
                {"date": s["date"] or "", "price": s["price"], "cond": s["title"][:60]}
                for s in sales
            ]
        except Exception as e:
            log.warning("ebay failed for %s (%s): %s", cardId, lang, e)
            await store.set(f"ebfail:{cardId}:{lang}", "1", FAIL_TTL)
            result["warnings"].append(f"Ventes eBay indisponibles ({e or type(e).__name__}).")

    # Les deux sources en PARALLÈLE, avec une échéance globale : au pire on renvoie
    # ce qu'on a déjà (estimation) au lieu de laisser l'appli attendre dans le vide.
    tasks = [asyncio.create_task(cardmarket_job()), asyncio.create_task(ebay_job())]
    done, pending = await asyncio.wait(tasks, timeout=PRICE_DEADLINE_SECONDS)
    for t in pending:
        t.cancel()
    if pending:
        result["warnings"].append("Délai dépassé : certaines sources n'ont pas répondu à temps.")
        if result["cardmarket"] is None:
            _cardmarket_fallback(result, card, "délai dépassé")

    if result["cardmarket"] is None and not result["ebaySales"]:
        raise HTTPException(502, "Aucune source de prix disponible pour cette carte actuellement. "
                                 + " ".join(result["warnings"]))
    degraded = bool(result.get("cardmarketIsFallback") or pending)
    return result, degraded


async def _get_card_cached(cardId: str) -> dict:
    card = await store.get(f"card:{cardId}")
    if not card:
        card = await card_search.get_card(cardId)
        if card:
            await store.set(f"card:{cardId}", card, CARD_TTL)
    if not card:
        raise HTTPException(404, "Carte introuvable.")
    return dict(card)


def _aggregate_result(card: dict) -> dict:
    """Réponse INSTANTANÉE : prix Cardmarket moyen (TCGdex), décliné par état. Aucune lecture de page."""
    result = {"card": card, "mode": "aggregate", "cardmarket": None, "ebaySales": [],
              "warnings": [], "availableLanguages": []}
    p = card.get("fallbackCardmarketPrice")
    if p:
        flat = {k: [round(float(p) * COND_FACTOR[k], 2)] for k in COND_FACTOR}
        result["cardmarket"] = {l: dict(flat) for l in SUPPORTED_LANGS}
        result["availableLanguages"] = SUPPORTED_LANGS
        result["cardmarketIsFallback"] = True
    return result


async def _compute_and_store(key: str, cardId: str, lang: str, force: bool = False) -> dict:
    result, degraded = await _compute_price(cardId, lang, force)
    entry = {"t": time.time(), "degraded": degraded, "v": result}
    await store.set(key, entry, PRICE_STALE_MAX)
    return entry


def _start_refresh(key: str, cardId: str, lang: str, force: bool = False) -> asyncio.Task:
    """Une seule mise à jour à la fois par (carte, langue) : évite de payer deux fois les mêmes pages."""
    task = _inflight.get(key)
    if task is None:
        task = asyncio.create_task(_compute_and_store(key, cardId, lang, force))
        _inflight[key] = task

        def _done(t, key=key):
            _inflight.pop(key, None)
            if not t.cancelled() and t.exception():
                log.warning("mise à jour %s échouée: %s", key, t.exception())
        task.add_done_callback(_done)
    return task


def _serve(entry: dict, refreshing: bool = False) -> dict:
    out = dict(entry["v"])
    out["fetchedAt"] = entry["t"]
    out["ageSeconds"] = int(time.time() - entry["t"])
    out["refreshing"] = refreshing
    return out


@app.get("/price")
async def price(cardId: str = Query(...), lang: str = Query("FR"),
                precise: int = Query(0), force: int = Query(0)):
    """
    precise=0 (défaut) : réponse immédiate — si une lecture détaillée de cette carte/langue est déjà
                         en cache (24 h) elle est renvoyée, sinon prix Cardmarket moyen (toutes langues).
    precise=1          : lit les annonces Cardmarket de la langue (5 plus bas prix par état) + eBay ;
                         lent (jusqu'à ~1 min) la première fois, instantané ensuite pendant 24 h.
    force=1            : ignore le cache et la mémoire des échecs récents (bouton « Réessayer »).
    """
    lang = lang.upper() if lang.upper() in SUPPORTED_LANGS else "FR"
    key = f"price:{cardId}:{lang}"
    entry = await store.get(key)
    if entry and not force:
        age = time.time() - entry["t"]
        if age < (DEGRADED_FRESH if entry["degraded"] else PRICE_FRESH):
            return _serve(entry)                           # frais : instantané
        if not entry["degraded"] and age < PRICE_STALE_MAX:
            _start_refresh(key, cardId, lang)              # périmé : on répond tout de suite
            return _serve(entry, refreshing=True)          # et on met à jour en arrière-plan

    if not precise:
        card = await _get_card_cached(cardId)
        out = _aggregate_result(card)
        out["ageSeconds"] = 0
        return out

    entry = await asyncio.shield(_start_refresh(key, cardId, lang, bool(force)))
    return _serve(entry)


def _cardmarket_fallback(result: dict, card: dict, reason: str):
    result["warnings"].append(
        f"Prix Cardmarket exact indisponible ({reason}). Prix ESTIMÉ à partir du prix moyen "
        "Cardmarket, avec un coefficient par état — non spécifique à une langue."
    )
    if card.get("fallbackCardmarketPrice"):
        # Seul un prix moyen existe : on le décline par état avec des coefficients
        # indicatifs (le prix moyen correspond à peu près à Near Mint). ESTIMATION.
        p = float(card["fallbackCardmarketPrice"])
        flat = {k: [round(p * COND_FACTOR[k], 2)] for k in COND_FACTOR}
        result["cardmarket"] = {l: dict(flat) for l in SUPPORTED_LANGS}
        result["availableLanguages"] = SUPPORTED_LANGS
        result["cardmarketIsFallback"] = True


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
async def debug_rows_ep(url: str, sel: str | None = None):
    """Chemin réel de récupération : statut, nombre de lignes, HTML de la 1re annonce, résultat parsé."""
    _check_debug_url(url)
    try:
        return await debug_rows(url, sel)
    except Exception as e:
        raise HTTPException(502, f"Lecture impossible : {e}")


@app.get("/debug/config", include_in_schema=False)
async def debug_config():
    return {"scrapingService": uses_scraping_service(), "persistentCache": store.persistent()}


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
