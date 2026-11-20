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
from browser_pool import new_page

# --- Sélecteurs à vérifier/ajuster en premier si le scraping échoue ---
ITEM_SELECTOR = "li.s-item"
TITLE_SELECTOR = ".s-item__title"
PRICE_SELECTOR = ".s-item__price"
DATE_SELECTOR = ".s-item__caption, .POSITIVE"
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
    m = re.search(r"([\d.,]+)\s*€", txt) or re.search(r"EUR\s*([\d.,]+)", txt)
    if not m:
        return None
    raw = m.group(1).replace(".", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


async def get_ebay_sold(query: str, limit: int = 5, lang: str | None = None) -> list[dict]:
    """Renvoie une liste de {title, price, date} pour les dernières ventes conclues.
    `lang` (FR/EN/DE/IT/ES/JP) ajoute un mot-clé de langue à la recherche
    (voir LANG_KEYWORDS) — best-effort, voir note en haut du fichier."""
    full_query = query
    if lang and lang in LANG_KEYWORDS:
        full_query = f"{query} {LANG_KEYWORDS[lang]}"
    url = SEARCH_URL.format(query=quote_plus(full_query))
    context, page = await new_page()
    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        if resp is None or resp.status >= 400:
            raise EbayScrapeError(f"eBay a répondu avec le statut {resp.status if resp else '?'}")

        try:
            await page.wait_for_selector(ITEM_SELECTOR, timeout=8000)
        except Exception:
            raise EbayScrapeError(
                "Résultats introuvables — eBay a peut-être affiché une page de "
                "vérification/CAPTCHA, ou la structure de la page a changé."
            )

        items = await page.query_selector_all(ITEM_SELECTOR)
        out = []
        for item in items:
            if len(out) >= limit:
                break
            title_el = await item.query_selector(TITLE_SELECTOR)
            price_el = await item.query_selector(PRICE_SELECTOR)
            date_el = await item.query_selector(DATE_SELECTOR)
            if not title_el or not price_el:
                continue
            title = (await title_el.inner_text()).strip()
            if title.lower().startswith("objet d'occasion") and len(title) < 5:
                continue  # ligne d'en-tête/placeholder eBay
            price = _parse_price_eur(await price_el.inner_text())
            date = (await date_el.inner_text()).strip() if date_el else None
            if price is not None:
                out.append({"title": title, "price": price, "date": date})

        if not out:
            raise EbayScrapeError("Aucune vente exploitable trouvée dans les résultats.")
        return out
    finally:
        await context.close()
