"""
Cardmarket — prix des annonces PAR LANGUE puis PAR ÉTAT (MT/NM/EX/GD/LP/PL/PO),
jusqu'à 5 plus bas prix par (langue, état), comme demandé.

Pour que le prix soit celui de la langue choisie, la page produit est chargée
avec le filtre de langue de Cardmarket (?language=<code>) ; chaque annonce est
de toute façon re-classée selon l'icône de langue lue dans la ligne.

⚠️ NON TESTÉ sur le vrai site (pas d'accès réseau depuis l'environnement de
développement). Les sélecteurs ci-dessous sont des estimations ; utilise
/debug/rows (main.py) pour voir le HTML réel d'une ligne et les ajuster.
Le parsing (parse_listings) est, lui, testé hors ligne sur du HTML d'exemple.
"""
import re
from urllib.parse import quote_plus, urlparse, urlencode, parse_qsl, urlunparse
from bs4 import BeautifulSoup
from browser_pool import fetch_html, explain_http_error, new_page

# --- Sélecteurs à vérifier/ajuster en premier si le scraping échoue ---
ROW_SELECTOR = "div.article-row"
CONDITION_SELECTOR = "span.article-condition, a.article-condition, span[data-condition], .product-attributes .badge"
PRICE_SELECTOR = "span.price-container, span.color-primary, .mobile-offer-container .color-primary"
LANGUAGE_SELECTOR = "span[data-language], span.icon[title], span.icon[data-bs-original-title], img[alt]"
# -----------------------------------------------------------------------

CONDITIONS = ["MT", "NM", "EX", "GD", "LP", "PL", "PO"]
CONDITION_MAP = {
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
# Codes de langue du filtre Cardmarket (?language=) — à vérifier sur le site.
LANG_CODE = {"EN": 1, "FR": 2, "DE": 3, "ES": 4, "IT": 5, "JP": 7}

_ATTRS = ("title", "data-bs-original-title", "data-original-title", "aria-label", "alt")


class CardmarketScrapeError(Exception):
    pass


def _texts(el) -> list[str]:
    out = [el.get_text(" ", strip=True)]
    for a in _ATTRS:
        v = el.get(a)
        if v:
            out.append(str(v))
    return [t for t in out if t]


def _parse_price(txt: str) -> float | None:
    m = re.search(r"([\d.,]+)\s*€", txt or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(".", "").replace(",", "."))
    except ValueError:
        return None


def _parse_condition(el) -> str | None:
    for t in _texts(el):
        m = re.search(r"\b(MT|NM|EX|GD|LP|PL|PO)\b", t.upper())
        if m:
            return m.group(1)
        k = CONDITION_MAP.get(t.strip().lower())
        if k:
            return k
    return None


def _parse_language(el) -> str | None:
    for t in _texts(el):
        k = LANGUAGE_MAP.get(t.strip().lower())
        if k:
            return k
    return None


def parse_listings(html: str, per_bucket_limit: int = 5) -> dict:
    """HTML d'une page produit -> {langue: {état: [5 plus bas prix]}} (langues non vides seulement)."""
    soup = BeautifulSoup(html, "lxml")
    buckets = {lang: {c: [] for c in CONDITIONS} for lang in SUPPORTED_LANGS}
    for row in soup.select(ROW_SELECTOR):
        cond_el = row.select_one(CONDITION_SELECTOR)
        price_el = row.select_one(PRICE_SELECTOR)
        lang_el = row.select_one(LANGUAGE_SELECTOR)
        if not (cond_el and price_el and lang_el):
            continue
        cond = _parse_condition(cond_el)
        lang = _parse_language(lang_el)
        price = _parse_price(price_el.get_text(" ", strip=True))
        if cond and lang and price is not None:
            buckets[lang][cond].append(price)
    out = {}
    for lang, conds in buckets.items():
        for c in conds:
            conds[c] = sorted(conds[c])[:per_bucket_limit]
        if any(conds.values()):
            out[lang] = conds
    return out


def _with_language(url: str, lang: str | None) -> str:
    code = LANG_CODE.get((lang or "").upper())
    if not code:
        return url
    u = urlparse(url)
    q = dict(parse_qsl(u.query))
    q["language"] = str(code)
    return urlunparse(u._replace(query=urlencode(q)))


async def get_cardmarket_prices(product_url: str, lang: str | None = None, per_bucket_limit: int = 5) -> dict:
    if not product_url:
        raise CardmarketScrapeError("Aucune URL Cardmarket fournie pour cette carte.")
    status, html, info = await fetch_html(_with_language(product_url, lang), wait_selector=ROW_SELECTOR)
    if status != 200:
        raise CardmarketScrapeError(explain_http_error(status, info))
    result = parse_listings(html, per_bucket_limit)
    if not result:
        raise CardmarketScrapeError(
            "page chargée mais aucune annonce lisible (structure différente de celle attendue, "
            "ou page de vérification) — voir /debug/rows")
    return result


# ---------------------------------------------------------------------------
# Retrouver la page Cardmarket d'une carte quand aucun lien officiel n'est connu
# ---------------------------------------------------------------------------
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
        score += int(10 * sum(1 for t in st if t in h) / len(st))
    nt = _tokens(name)
    if nt and all(t in h for t in nt):
        score += 3
    num = str(number or "").lstrip("0")
    if num and re.search(rf"(?<![0-9a-z]){re.escape(num)}(?![0-9a-z])", f"{h}-{txt}"):
        score += 4
    return score


def pick_product_url(html: str, name: str, set_name: str | None, number: str | None) -> str:
    soup = BeautifulSoup(html, "lxml")
    best, best_score, seen = None, -1, set()
    for a in soup.select(PRODUCT_LINK_SELECTOR):
        href = a.get("href", "")
        if href.startswith("/"):
            href = "https://www.cardmarket.com" + href
        if href in seen:
            continue
        seen.add(href)
        row = a.find_parent(["tr", "div"])
        text = row.get_text(" ", strip=True) if row else a.get_text(" ", strip=True)
        sc = _score_candidate(href, text, name, set_name or "", number or "")
        if sc > best_score:
            best, best_score = href, sc
    if not best:
        raise CardmarketScrapeError("aucun résultat de recherche lisible (page de vérification ou structure différente)")
    if best_score < 8:
        raise CardmarketScrapeError(
            f"aucun résultat ne correspond à « {name} » dans « {set_name} » (meilleur score {best_score})")
    return best


async def find_product_url(name: str, set_name: str | None, number: str | None) -> str:
    status, html, info = await fetch_html(SEARCH_URL.format(q=quote_plus(name)), wait_selector=PRODUCT_LINK_SELECTOR)
    if status != 200:
        raise CardmarketScrapeError("recherche Cardmarket : " + explain_http_error(status, info))
    return pick_product_url(html, name, set_name, number)


# --------------------------------------------------------------- diagnostic
async def snapshot(url: str, png: bool = False):
    """Ce que voit le navigateur LOCAL du serveur (capture ou résumé texte)."""
    context, page = await new_page()
    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        await page.wait_for_timeout(2500)
        if png:
            return await page.screenshot(type="png")
        return {
            "status": resp.status if resp else None,
            "finalUrl": page.url,
            "title": await page.title(),
            "rowCount": len(await page.query_selector_all(ROW_SELECTOR)),
            "textStart": (await page.inner_text("body"))[:1500],
        }
    finally:
        await context.close()


async def debug_rows(url: str) -> dict:
    """Chemin réel de récupération (service ou navigateur) : statut, nb de lignes, HTML de la 1re ligne."""
    status, html, info = await fetch_html(url, wait_selector=ROW_SELECTOR)
    soup = BeautifulSoup(html or "", "lxml")
    rows = soup.select(ROW_SELECTOR)
    return {
        "status": status, "info": info, "htmlLength": len(html or ""),
        "title": soup.title.get_text(strip=True) if soup.title else None,
        "rowCount": len(rows),
        "firstRowHtml": str(rows[0])[:3000] if rows else None,
        "parsed": parse_listings(html or "") if status == 200 else None,
        "textStart": soup.get_text(" ", strip=True)[:600],
    }
