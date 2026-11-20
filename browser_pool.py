"""
Navigateur headless partagé (Playwright + Chromium) pour les deux scrapers.

On passe par un vrai navigateur (plutôt que de simples requêtes HTTP) car
eBay et Cardmarket utilisent tous deux des protections anti-robot qui
bloquent facilement les clients HTTP basiques. Rien ne garantit que ça
suffise à long terme (ces protections évoluent) — si les scrapers se
mettent à échouer, c'est le premier endroit à vérifier.
"""
from playwright.async_api import async_playwright, Browser

_playwright = None
_browser: Browser | None = None

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)


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


async def shutdown():
    global _playwright, _browser
    if _browser:
        await _browser.close()
        _browser = None
    if _playwright:
        await _playwright.stop()
        _playwright = None
