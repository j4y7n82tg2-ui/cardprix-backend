"""
Scraping des ventes eBay conclues ("sold / completed listings") pour une
carte donnée, sur ebay.fr (prix en euros, plus pertinent pour une revente
sur Vinted que ebay.com en dollars).

⚠️ MÊME AVERTISSEMENT que pour scraper_cardmarket.py : les sélecteurs
ci-dessous sont une estimation non testée en conditions réelles (pas
d'accès réseau vers ebay.fr depuis ce sandbox). Prévois de les ajuster
après le premier déploiement réel.

Risque spécifique à eBay : la recherche "objets vendus" est régulièrement
la cible de restrictions anti-robot plus strictes que le reste du site, et
l'accès à ce filtre a été restreint par le passé selon les pays/comptes.
Si ce scraper échoue systématiquement, la solution de repli raisonnable
est de retirer la section "Dernières ventes eBay" plutôt que d'insister
avec des techniques plus agressives (rotation de proxys, etc.) qui
augmentent le risque de blocage et la zone grise CGU.
"""
import re
from urllib.parse import quote_plus
from bs4 import BeautifulSoup
from browser_pool import fetch_html, explain_http_error

# --- Sélecteurs à vérifier/ajuster en premier si le scraping échoue ---
ITEM_SELECTOR = "li.s-item, li.s-card, ul.srp-results > li"
TITLE_SELECTOR = ".s-item__title, .s-card__title"
PRICE_SELECTOR = ".s-item__price, .s-card__price"
DATE_SELECTOR = ".s-item__caption, .s-card__caption, .POSITIVE"
# -----------------------------------------------------------------------

SEARCH_URL = (
    "https://www.ebay.fr/sch/i.html?_nkw={query}&_sacat=0"
    "&LH_Sold=1&LH_Complete=1&_sop=13"
)

# eBay n'offre pas de filtre "langue de la carte" fiable, donc on biaise la
# recherche en ajoutant un mot-clé à la requête texte — best-effort, pas une
# garantie que chaque résultat soit vraiment dans cette langue (un vendeur
# peut omettre la précision). ULTRA IMPORTANT côté produit : une carte
# anglaise et française n'ont pas la même cote, donc ce biais est nécessaire
# même imparfait.
LANG_KEYWORDS = {
    "FR": "française VF",
    "EN": "english anglaise",
    "DE": "deutsch allemande",
    "IT": "italiana italienne",
    "ES": "española espagnole",
    "JP": "japanese japonaise",
}


class EbayScrapeError(Exception):
    pass


def _parse_price_eur(txt: str) -> float | None:
    if not txt:
        return None
    m = (re.search(r"([\d.,]+)\s*(?:€|EUR)", txt) or re.search(r"(?:€|EUR)\s*([\d.,]+)", txt))
    if not m:
        return None
    raw = m.group(1).replace(".", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def parse_sold(html: str, limit: int = 5) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    out = []
    for item in soup.select(ITEM_SELECTOR):
        if len(out) >= limit:
            break
        title_el = item.select_one(TITLE_SELECTOR)
        price_el = item.select_one(PRICE_SELECTOR)
        date_el = item.select_one(DATE_SELECTOR)
        if not title_el or not price_el:
            continue
        title = title_el.get_text(" ", strip=True)
        price = _parse_price_eur(price_el.get_text(" ", strip=True))
        date = date_el.get_text(" ", strip=True) if date_el else None
        if price is not None and title:
            out.append({"title": title, "price": price, "date": date})
    return out


async def get_ebay_sold(query: str, limit: int = 5, lang: str | None = None) -> list[dict]:
    """Dernières ventes conclues [{title, price, date}] ; `lang` ajoute un mot-clé de langue (best-effort)."""
    full_query = query
    if lang and lang in LANG_KEYWORDS:
        full_query = f"{query} {LANG_KEYWORDS[lang]}"
    status, html, info = await fetch_html(SEARCH_URL.format(query=quote_plus(full_query)), wait_selector=ITEM_SELECTOR)
    if status != 200:
        raise EbayScrapeError(explain_http_error(status, info))
    out = parse_sold(html, limit)
    if not out:
        raise EbayScrapeError("aucune vente lisible (page de vérification ou structure différente)")
    return out
