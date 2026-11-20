"""
Recherche et identification de carte — via l'API publique Pokemon TCG
(https://pokemontcg.io), gratuite et stable (pas de scraping ici).

Cette API renvoie aussi, pour chaque carte, un lien direct vers sa fiche
Cardmarket officielle (`cardmarket.url`) : c'est ce lien qu'on réutilise
ensuite dans scraper_cardmarket.py, pour ne jamais avoir à deviner l'URL
Cardmarket d'une carte nous-mêmes (source d'erreurs sinon).

Elle fournit aussi un prix Cardmarket agrégé "gratuit" (trend/avg/low,
pas de détail par état) : on l'utilise comme solution de repli si notre
scraper échoue, pour ne jamais laisser l'utilisateur sans aucun prix.
"""
import httpx

POKEMONTCG_BASE = "https://api.pokemontcg.io/v2"


async def search_cards(query: str, limit: int = 15) -> list[dict]:
    """Cherche des cartes par nom. Renvoie une liste simplifiée pour le frontend."""
    if not query or len(query.strip()) < 2:
        return []

    params = {
        "q": f'name:"{query}*"',
        "pageSize": str(limit),
        "orderBy": "-set.releaseDate",
    }
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{POKEMONTCG_BASE}/cards", params=params)
        r.raise_for_status()
        data = r.json()

    out = []
    for c in data.get("data", []):
        out.append(_simplify(c))
    return out


def _simplify(c: dict) -> dict:
    cm = c.get("cardmarket") or {}
    set_info = c.get("set") or {}
    release_date = set_info.get("releaseDate") or ""  # format "YYYY/MM/DD"
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
        # Prix de repli (agrégé, pas par état) fourni gratuitement par l'API,
        # utilisé seulement si notre propre scraping Cardmarket échoue.
        "fallbackCardmarketPrice": (cm.get("prices") or {}).get("trendPrice")
                                    or (cm.get("prices") or {}).get("averageSellPrice"),
    }


async def get_card(card_id: str) -> dict | None:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{POKEMONTCG_BASE}/cards/{card_id}")
        if r.status_code != 200:
            return None
        c = r.json().get("data")
        if not c:
            return None
    return _simplify(c)
