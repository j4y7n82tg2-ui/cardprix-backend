"""
Recherche et identification de carte.

SOURCE PRINCIPALE : TCGdex (https://tcgdex.dev, API ouverte, à jour des séries
récentes — ex. 30th Celebration, sept. 2026). L'API pokemontcg.io, utilisée
avant, n'est plus mise à jour (séries 2026 absentes ou en erreur 500) ; elle
ne sert plus que de SECOURS si TCGdex ne répond pas, et pour retrouver le lien
Cardmarket officiel d'une carte quand il existe.

Les résultats sont triés de la série la plus RÉCENTE à la plus ancienne :
chercher "Lugia" renvoie d'abord la Lugia de la dernière extension sortie.

⚠️ Écrit à partir de la documentation publique de TCGdex, sans pouvoir
l'appeler depuis le bac à sable de développement (pas d'accès réseau) :
les champs réellement renvoyés peuvent différer légèrement — tout est lu avec
des .get() prudents pour ne jamais planter, mais vérifie /search?q=lugia.
"""
import os
import asyncio
import logging
import httpx

log = logging.getLogger("cardprix.cards")

TCGDEX = "https://api.tcgdex.net/v2/en"
POKEMONTCG_BASE = "https://api.pokemontcg.io/v2"
MAX_RESULTS = 40

# Clé API pokemontcg.io (optionnelle) pour le secours : variable POKEMONTCG_API_KEY.
_PTCG_HEADERS = {"X-Api-Key": os.environ["POKEMONTCG_API_KEY"]} if os.environ.get("POKEMONTCG_API_KEY") else {}

_set_cache: dict[str, dict] = {}   # id de set -> {name, releaseDate, series}


async def _get_json(url: str, params: dict | None = None, retries: int = 3,
                    headers: dict | None = None, timeout: float = 30):
    """GET avec nouvelles tentatives (erreurs passagères 429/5xx/réseau). 404 -> None."""
    last = None
    for attempt in range(retries):
        try:
            async with httpx.AsyncClient(timeout=timeout, headers=headers or {}) as client:
                r = await client.get(url, params=params)
            if r.status_code == 404:
                return None
            if r.status_code in (429, 500, 502, 503, 504):
                last = Exception(f"HTTP {r.status_code}")
            else:
                r.raise_for_status()
                return r.json()
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last = e
        await asyncio.sleep(1.2 * (attempt + 1))
    raise RuntimeError(f"{url.split('/')[2]} indisponible après {retries} essais ({last}).")


# ----------------------------------------------------------------- TCGdex ---

def _set_id_of(card_id: str) -> str:
    return card_id.rsplit("-", 1)[0] if "-" in card_id else card_id


async def _tcgdex_set(set_id: str) -> dict:
    """Infos d'une série (nom, date de sortie, collection), mises en cache."""
    if set_id in _set_cache:
        return _set_cache[set_id]
    info = {"name": None, "releaseDate": "", "series": None}
    try:
        data = await _get_json(f"{TCGDEX}/sets/{set_id}", retries=2, timeout=20) or {}
        info = {
            "name": data.get("name"),
            "releaseDate": data.get("releaseDate") or "",
            "series": (data.get("serie") or {}).get("name"),
        }
        _set_cache[set_id] = info
    except Exception as e:  # une série illisible ne doit pas casser toute la recherche
        log.warning("set %s illisible: %s", set_id, e)
    return info


def _img(base: str | None) -> str | None:
    # TCGdex renvoie l'URL sans extension : on ajoute la qualité/format.
    return f"{base}/low.webp" if base else None


def _num_key(local_id: str):
    digits = "".join(ch for ch in str(local_id) if ch.isdigit())
    return (int(digits) if digits else 10**9, str(local_id))


async def _search_tcgdex(query: str, limit: int) -> list[dict]:
    briefs = await _get_json(f"{TCGDEX}/cards", {"name": query}) or []
    briefs = [b for b in briefs if b.get("id")]
    if not briefs:
        return []

    set_ids = sorted({_set_id_of(b["id"]) for b in briefs})[:150]
    sem = asyncio.Semaphore(15)

    async def one(sid):
        async with sem:
            await _tcgdex_set(sid)

    await asyncio.gather(*(one(s) for s in set_ids))

    # Tri en deux passes (stable) : numéro croissant dans la série, puis
    # date de sortie de la série décroissante (la plus récente d'abord).
    briefs.sort(key=lambda b: _num_key(b.get("localId", "")))
    briefs.sort(key=lambda b: (_set_cache.get(_set_id_of(b["id"]), {}).get("releaseDate") or ""),
                reverse=True)

    out = []
    for b in briefs[:limit]:
        info = _set_cache.get(_set_id_of(b["id"]), {})
        rd = info.get("releaseDate") or ""
        out.append({
            "id": b["id"],
            "name": b.get("name"),
            "setName": info.get("name"),
            "series": info.get("series"),
            "number": b.get("localId"),
            "year": rd[:4] or None,
            "rarity": None,      # complété par /price (détail de la carte)
            "artist": None,
            "img": _img(b.get("image")),
            "cardmarketUrl": None,
            "fallbackCardmarketPrice": None,
        })
    return out


async def _get_card_tcgdex(card_id: str) -> dict | None:
    c = await _get_json(f"{TCGDEX}/cards/{card_id}")
    if not c:
        return None
    sid = (c.get("set") or {}).get("id") or _set_id_of(card_id)
    info = await _tcgdex_set(sid)
    rd = info.get("releaseDate") or ""
    cm = ((c.get("pricing") or {}).get("cardmarket")) or {}
    # Prix Cardmarket agrégés fournis par TCGdex (toutes langues et tous états
    # confondus, mis à jour chaque jour). Variante "holo" si la carte l'est, sinon normale.
    is_holo = "holo" in str(c.get("rarity", "")).lower()
    def pick(field):
        order = [f"{field}-holo", field] if is_holo else [field, f"{field}-holo"]
        for k in order:
            if cm.get(k):
                return cm[k]
        return None
    agg = {k: pick(k) for k in ("trend", "avg", "low", "avg7", "avg30")}
    agg["updated"] = cm.get("updated")
    agg["variant"] = "holo" if (is_holo and (cm.get("trend-holo") or cm.get("avg-holo"))) else "normal"
    price = agg["trend"] or agg["avg"] or agg["low"]
    if not any(agg[k] for k in ("trend", "avg", "low")):
        agg = None
    card = {
        "id": c.get("id", card_id),
        "name": c.get("name"),
        "setName": (c.get("set") or {}).get("name") or info.get("name"),
        "series": info.get("series"),
        "number": c.get("localId"),
        "year": rd[:4] or None,
        "rarity": c.get("rarity"),
        "artist": c.get("illustrator"),
        "img": _img(c.get("image")),
        "cardmarketUrl": None,
        "fallbackCardmarketPrice": price,
        "cardmarketPricing": agg,
    }
    # Lien Cardmarket officiel : TCGdex n'en fournit pas ; pokemontcg.io oui pour
    # les cartes qu'il connaît. On ne l'accepte que si nom ET numéro concordent
    # (sinon on risquerait de scraper les prix d'une AUTRE carte).
    try:
        p = await _get_json(f"{POKEMONTCG_BASE}/cards/{card_id}", retries=1,
                            headers=_PTCG_HEADERS, timeout=8)
        pc = (p or {}).get("data") or {}
        same = (str(pc.get("name", "")).lower() == str(card["name"]).lower()
                and str(pc.get("number", "")).lstrip("0") == str(card["number"]).lstrip("0"))
        if same:
            card["cardmarketUrl"] = (pc.get("cardmarket") or {}).get("url")
            if not card["fallbackCardmarketPrice"]:
                prices = (pc.get("cardmarket") or {}).get("prices") or {}
                card["fallbackCardmarketPrice"] = prices.get("trendPrice") or prices.get("averageSellPrice")
    except Exception:
        pass
    return card


# ---------------------------------------------- pokemontcg.io (secours) ---

def _simplify_ptcg(c: dict) -> dict:
    cm = c.get("cardmarket") or {}
    set_info = c.get("set") or {}
    release_date = set_info.get("releaseDate") or ""  # "YYYY/MM/DD"
    return {
        "id": c["id"],
        "name": c.get("name"),
        "setName": set_info.get("name"),
        "series": set_info.get("series"),
        "number": c.get("number"),
        "year": release_date.split("/")[0] if release_date else None,
        "rarity": c.get("rarity"),
        "artist": c.get("artist"),
        "img": (c.get("images") or {}).get("small"),
        "cardmarketUrl": cm.get("url"),
        "fallbackCardmarketPrice": (cm.get("prices") or {}).get("trendPrice")
                                    or (cm.get("prices") or {}).get("averageSellPrice"),
        "_rd": release_date,
    }


async def _search_ptcg(query: str, limit: int) -> list[dict]:
    params = {"q": f'name:"{query}*"', "pageSize": "250"}
    data = await _get_json(f"{POKEMONTCG_BASE}/cards", params, headers=_PTCG_HEADERS) or {}
    cards = [_simplify_ptcg(c) for c in data.get("data", [])]
    cards.sort(key=lambda c: c["_rd"], reverse=True)   # récent d'abord
    for c in cards:
        c.pop("_rd", None)
    return cards[:limit]


# ---------------------------------------------------------------- public ---

async def search_cards(query: str, limit: int = MAX_RESULTS) -> list[dict]:
    """Cherche des cartes par nom, séries les plus récentes d'abord."""
    if not query or len(query.strip()) < 2:
        return []
    query = query.strip()
    try:
        cards = await _search_tcgdex(query, limit)
        if cards:
            return cards
        log.info("TCGdex: aucun résultat pour %r, essai pokemontcg.io", query)
    except Exception as e:
        log.warning("TCGdex indisponible (%s), secours pokemontcg.io", e)
    return await _search_ptcg(query, limit)


async def get_card(card_id: str) -> dict | None:
    try:
        card = await _get_card_tcgdex(card_id)
        if card:
            return card
    except Exception as e:
        log.warning("TCGdex get_card %s: %s", card_id, e)
    data = await _get_json(f"{POKEMONTCG_BASE}/cards/{card_id}", headers=_PTCG_HEADERS)
    c = (data or {}).get("data")
    if not c:
        return None
    card = _simplify_ptcg(c)
    card.pop("_rd", None)
    return card


async def raw_tcgdex_card(card_id: str) -> dict | None:
    """Diagnostic : réponse brute de TCGdex pour une carte (sans les attaques)."""
    c = await _get_json(f"{TCGDEX}/cards/{card_id}")
    if isinstance(c, dict):
        for k in ("attacks", "abilities", "description"):
            c.pop(k, None)
    return c
