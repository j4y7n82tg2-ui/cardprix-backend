"""
Scraping de la page produit Cardmarket pour récupérer, PAR LANGUE puis PAR
ÉTAT (Mint / Near Mint / Excellent / Good / Light Played / Played / Poor),
les prix des annonces actuellement en vente les plus basses — ce qui
correspond à la demande d'origine : "les 5 premiers prix par état", avec en
plus le filtre par langue qui a été demandé ensuite (ULTRA IMPORTANT côté
produit : une carte française et une carte anglaise n'ont pas la même cote).

La page produit Cardmarket liste déjà les annonces de TOUTES les langues
mélangées, chaque ligne précisant la langue de l'exemplaire (icône drapeau).
On scrape donc la page UNE SEULE FOIS et on regroupe par (langue, état) —
pas besoin de rescraper à chaque changement de langue côté appli.

⚠️ IMPORTANT — à lire avant de déployer :
Les sélecteurs CSS ci-dessous sont une estimation raisonnable de la
structure du site au moment de l'écriture (nov. 2026), construite à partir
de la description publique de la page, PAS d'un test en conditions réelles
(ce sandbox n'a pas accès réseau vers cardmarket.com pour vérifier).
Il est très probable qu'il faille ajuster `ROW_SELECTOR` / `CONDITION_SELECTOR`
/ `PRICE_SELECTOR` / `LANGUAGE_SELECTOR` après un premier essai réel — ouvre
la page produit dans un navigateur, inspecte le tableau des annonces, et
corrige ces constantes en conséquence. Le reste du fichier n'a pas besoin
de changer.

Si Cardmarket bloque la requête (page de vérification / erreur), la fonction
lève CardmarketScrapeError — l'appelant (main.py) doit alors se replier sur
`fallbackCardmarketPrice` fourni par l'API Pokemon TCG (qui n'est pas filtré
par langue — c'est un repli dégradé, à signaler clairement dans l'appli).
"""
import re
from browser_pool import new_page

# --- Sélecteurs à vérifier/ajuster en premier si le scraping échoue ---
ROW_SELECTOR = "div.article-row"          # une ligne d'annonce dans le tableau
CONDITION_SELECTOR = "span.article-condition, span[data-condition]"
PRICE_SELECTOR = "span.price-container, span.color-primary"
# L'icône de langue est généralement une image/span avec un attribut
# title/alt du type "Français", "English", "FR", "EN"...
LANGUAGE_SELECTOR = "span[data-language] img, span.icon[title], img[alt]"
# -----------------------------------------------------------------------

CONDITION_MAP = {
    "MT": "MT", "NM": "NM", "EX": "EX", "GD": "GD",
    "LP": "LP", "PL": "PL", "PO": "PO",
    "mint": "MT", "near mint": "NM", "excellent": "EX", "good": "GD",
    "light played": "LP", "played": "PL", "poor": "PO",
}

LANGUAGE_MAP = {
    "fr": "FR", "fra": "FR", "français": "FR", "francais": "FR", "french": "FR",
    "en": "EN", "eng": "EN", "english": "EN", "anglais": "EN",
    "de": "DE", "ger": "DE", "deutsch": "DE", "german": "DE", "allemand": "DE",
    "it": "IT", "ita": "IT", "italiano": "IT", "italian": "IT", "italien": "IT",
    "es": "ES", "spa": "ES", "español": "ES", "spanish": "ES", "espagnol": "ES",
    "jp": "JP", "jap": "JP", "japanese": "JP", "japonais": "JP", "日本語": "JP",
}

SUPPORTED_LANGS = ["FR", "EN", "DE", "IT", "ES", "JP"]


class CardmarketScrapeError(Exception):
    pass


def _parse_price(txt: str) -> float | None:
    if not txt:
        return None
    # Cardmarket affiche les prix en "12,34 €" (format EU)
    m = re.search(r"([\d.,]+)\s*€", txt)
    if not m:
        return None
    raw = m.group(1).replace(".", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_condition(txt: str) -> str | None:
    if not txt:
        return None
    key = txt.strip().lower()
    return CONDITION_MAP.get(key) or CONDITION_MAP.get(txt.strip())


def _parse_language(txt: str) -> str | None:
    if not txt:
        return None
    return LANGUAGE_MAP.get(txt.strip().lower())


async def get_cardmarket_prices(product_url: str, per_bucket_limit: int = 5) -> dict:
    """
    Renvoie {"FR": {"MT": [prix,...], "NM": [...], ...}, "EN": {...}, ...} —
    jusqu'à `per_bucket_limit` prix les plus bas trouvés pour chaque
    (langue, état), triés croissant. Une langue absente du résultat signifie
    qu'aucune annonce dans cette langue n'a été trouvée sur la page.
    """
    if not product_url:
        raise CardmarketScrapeError("Aucune URL Cardmarket fournie pour cette carte.")

    context, page = await new_page()
    try:
        resp = await page.goto(product_url, wait_until="domcontentloaded", timeout=20000)
        if resp is None or resp.status >= 400:
            raise CardmarketScrapeError(f"Cardmarket a répondu avec le statut {resp.status if resp else '?'}")

        try:
            await page.wait_for_selector(ROW_SELECTOR, timeout=8000)
        except Exception:
            raise CardmarketScrapeError(
                "Tableau des annonces introuvable — la page a peut-être changé de "
                "structure, ou Cardmarket a affiché une page de vérification."
            )

        rows = await page.query_selector_all(ROW_SELECTOR)
        buckets: dict[str, dict[str, list[float]]] = {
            lang: {k: [] for k in ["MT", "NM", "EX", "GD", "LP", "PL", "PO"]}
            for lang in SUPPORTED_LANGS
        }

        for row in rows:
            cond_el = await row.query_selector(CONDITION_SELECTOR)
            price_el = await row.query_selector(PRICE_SELECTOR)
            lang_el = await row.query_selector(LANGUAGE_SELECTOR)
            if not cond_el or not price_el or not lang_el:
                continue
            cond = _parse_condition(await cond_el.inner_text())
            price = _parse_price(await price_el.inner_text())
            lang_raw = (await lang_el.get_attribute("title")
                        or await lang_el.get_attribute("alt")
                        or await lang_el.inner_text())
            lang = _parse_language(lang_raw)
            if not (cond and lang and price is not None):
                continue
            bucket = buckets[lang][cond]
            if len(bucket) < per_bucket_limit:
                bucket.append(price)

        for lang in buckets:
            for cond in buckets[lang]:
                buckets[lang][cond].sort()

        # On ne garde que les langues pour lesquelles on a trouvé au moins un prix,
        # pour que le frontend sache clairement quelles langues ont des données.
        non_empty = {
            lang: conds for lang, conds in buckets.items()
            if any(conds.values())
        }

        if not non_empty:
            raise CardmarketScrapeError("Aucune annonce exploitable trouvée sur la page.")

        return non_empty
    finally:
        await context.close()
