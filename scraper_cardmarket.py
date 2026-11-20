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


# ======================================================================
# Retrouver la page Cardmarket d'une carte (quand aucun lien officiel n'est
# connu, ex. séries très récentes absentes de pokemontcg.io).
#
# Méthode : recherche Cardmarket par nom, puis on choisit le résultat dont
# l'URL contient le nom de la SÉRIE (les URL Cardmarket ont la forme
# /Pokemon/Products/Singles/<Série>/<Carte>) et, si possible, le numéro.
# ⚠️ Non testé en réel : voir /debug/shot et /debug/info dans main.py pour
# voir ce que ton serveur obtient réellement de Cardmarket.
# ======================================================================
SEARCH_URL = "https://www.cardmarket.com/fr/Pokemon/Products/Search?searchString={q}"
PRODUCT_LINK_SELECTOR = 'a[href*="/Pokemon/Products/Singles/"]'


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def _tokens(s: str) -> list[str]:
    return [t for t in _slug(s).split("-") if len(t) >= 3 or t.isdigit()]


def _score_candidate(href: str, text: str, name: str, set_name: str, number: str) -> int:
    h = _slug(href.split("/Singles/", 1)[-1])
    txt = _slug(text)
    score = 0
    st = _tokens(set_name)
    if st:
        score += int(10 * sum(1 for t in st if t in h) / len(st))   # nom de série dans l'URL
    nt = _tokens(name)
    if nt and all(t in h for t in nt):
        score += 3
    num = str(number or "").lstrip("0")
    if num and re.search(rf"(?<![0-9a-z]){re.escape(num)}(?![0-9a-z])", f"{h}-{txt}"):
        score += 4
    return score


async def find_product_url(name: str, set_name: str | None, number: str | None) -> str:
    from urllib.parse import quote_plus
    context, page = await new_page()
    try:
        resp = await page.goto(SEARCH_URL.format(q=quote_plus(name)),
                               wait_until="domcontentloaded", timeout=25000)
        if resp is None or resp.status >= 400:
            raise CardmarketScrapeError(
                f"recherche Cardmarket refusée (statut {resp.status if resp else '?'}) — "
                "probablement une protection anti-robot sur l'adresse du serveur")
        try:
            await page.wait_for_selector(PRODUCT_LINK_SELECTOR, timeout=10000)
        except Exception:
            raise CardmarketScrapeError(
                "aucun résultat de recherche lisible (page de vérification anti-robot, "
                "ou structure de la page différente)")
        cands = await page.eval_on_selector_all(
            PRODUCT_LINK_SELECTOR,
            "els => els.map(e => ({href: e.href, text: (e.closest('tr, .row, [class*=\"table-body\"] > div') || e.parentElement || e).innerText || ''}))")
        best, best_score, seen = None, -1, set()
        for c in cands:
            if c["href"] in seen:
                continue
            seen.add(c["href"])
            sc = _score_candidate(c["href"], c["text"], name, set_name or "", number or "")
            if sc > best_score:
                best, best_score = c["href"], sc
        if not best or best_score < 8:
            raise CardmarketScrapeError(
                f"aucun résultat Cardmarket ne correspond à « {name} » dans « {set_name} » "
                f"(meilleur score {best_score})")
        return best
    finally:
        await context.close()


async def snapshot(url: str, png: bool = False):
    """Outil de diagnostic : ce que le navigateur du serveur voit sur une page."""
    context, page = await new_page()
    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        await page.wait_for_timeout(2500)
        if png:
            return await page.screenshot(type="png")
        text = (await page.inner_text("body"))[:1500]
        return {
            "status": resp.status if resp else None,
            "finalUrl": page.url,
            "title": await page.title(),
            "rowCount": len(await page.query_selector_all(ROW_SELECTOR)),
            "productLinks": len(await page.query_selector_all(PRODUCT_LINK_SELECTOR)),
            "textStart": text,
        }
    finally:
        await context.close()
