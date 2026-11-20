"""
Récupération des pages web pour les scrapers.

Deux modes :
 1. SERVICE ANTI-BLOCAGE (recommandé) : si la variable d'environnement
    SCRAPINGBEE_API_KEY est définie, les pages passent par ScrapingBee
    (https://www.scrapingbee.com) qui utilise des adresses non bloquées par
    Cardmarket/eBay. Coût en crédits par page, plafonné par
    SCRAPINGBEE_MAX_COST (défaut 25 ; 75 pour les protections les plus dures).
 2. NAVIGATEUR LOCAL (gratuit, sinon) : Playwright + Chromium depuis le serveur.
    Cardmarket bloque en général les adresses de serveurs (erreur 401/403).

`fetch_html(url)` renvoie toujours (statut_http, html, infos) — c'est tout ce
dont les scrapers ont besoin, le parsing se fait ensuite avec BeautifulSoup.
"""
import os
import httpx
from playwright.async_api import async_playwright, Browser

_playwright = None
_browser: Browser | None = None

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)
SCRAPINGBEE_URL = "https://app.scrapingbee.com/api/v1/"


def uses_scraping_service() -> bool:
    return bool(os.environ.get("SCRAPINGBEE_API_KEY"))


async def get_browser() -> Browser:
    global _playwright, _browser
    if _browser is None:
        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
    return _browser


async def new_page(locale="fr-FR", timezone="Europe/Paris"):
    browser = await get_browser()
    context = await browser.new_context(
        user_agent=USER_AGENT,
        locale=locale,
        timezone_id=timezone,
        viewport={"width": 1280, "height": 900},
    )
    page = await context.new_page()
    return context, page


async def fetch_html(url: str, wait_selector: str | None = None) -> tuple[int, str, dict]:
    """(statut, html, infos). Ne lève pas d'exception sur un statut d'erreur."""
    key = os.environ.get("SCRAPINGBEE_API_KEY")
    if key:
        max_cost = os.environ.get("SCRAPINGBEE_MAX_COST", "25")
        async with httpx.AsyncClient(timeout=75) as client:
            r = await client.get(
                SCRAPINGBEE_URL,
                params={"url": url, "mode": "auto", "max_cost": max_cost},
                headers={"Authorization": f"Bearer {key}"},
            )
        return r.status_code, r.text, {
            "via": "scrapingbee",
            "cost": r.headers.get("Spb-cost"),
            "targetStatus": r.headers.get("Spb-initial-status-code"),
            "finalUrl": r.headers.get("Spb-resolved-url"),
        }
    context, page = await new_page()
    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        if wait_selector:
            try:
                await page.wait_for_selector(wait_selector, timeout=8000)
            except Exception:
                pass
        return (resp.status if resp else 0), await page.content(), {"via": "chromium", "finalUrl": page.url}
    finally:
        await context.close()


def explain_http_error(status: int, info: dict) -> str:
    if info.get("via") == "scrapingbee":
        if status == 401:
            return "ScrapingBee : plus de crédits (ou clé invalide)"
        if status == 429:
            return "ScrapingBee : trop de requêtes en même temps"
        if status == 500:
            return "ScrapingBee n'a pas réussi à charger la page (protection trop forte pour le niveau de crédits autorisé ?)"
        return f"ScrapingBee a répondu {status}"
    if status in (401, 403):
        return f"accès refusé (statut {status}) — protection anti-robot sur l'adresse du serveur"
    return f"statut HTTP {status}"


async def shutdown():
    global _playwright, _browser
    if _browser:
        await _browser.close()
        _browser = None
    if _playwright:
        await _playwright.stop()
        _playwright = None
