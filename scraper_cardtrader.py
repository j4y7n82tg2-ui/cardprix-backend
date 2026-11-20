"""
Prix par ÉTAT et par LANGUE via l'API CardTrader (gratuite avec un compte).

CardTrader est une place de marché européenne de cartes (concurrente de
Cardmarket) : ce ne sont donc PAS les annonces Cardmarket, mais de vraies
annonces à vendre, avec état et langue, en EUR. Aucun scraping, aucun blocage
anti-robot, aucun service payant.

Jeton : CardTrader > Paramètres > API (variable d'environnement CARDTRADER_TOKEN).
Limite documentée : GET /marketplace/products = 1 appel par seconde.

⚠️ Écrit d'après la documentation publique (champs : price.cents / price.currency,
properties_hash.condition, langue dans properties_hash, blueprints/export,
card_market_id), SANS pouvoir appeler l'API depuis le bac à sable. Les noms de
champs de langue / numéro sont donc cherchés de façon tolérante, et
/debug/cardtrader?id=...&lang=FR montre ce que l'API renvoie réellement.
"""
import os
import re
import time
import asyncio
import logging
import httpx

import cache as store

log = logging.getLogger("cardprix.cardtrader")

BASE = "https://api.cardtrader.com/api/v2"
TOKEN = os.environ.get("CARDTRADER_TOKEN", "").strip()
USD_EUR = float(os.environ.get("CARDTRADER_USD_EUR", "0.92"))
STATIC_TTL = 30 * 24 * 3600

# Langues de l'appli -> codes CardTrader (plusieurs variantes possibles pour le japonais).
LANG_CODES = {"FR": ["fr"], "EN": ["en"], "DE": ["de"], "IT": ["it"], "ES": ["es"], "JP": ["jp", "ja"]}

# État CardTrader -> état Cardmarket (7 niveaux de l'appli).
COND_MAP = {
    "mint": "MT",
    "near mint": "NM",
    "slightly played": "EX",
    "excellent": "EX",
    "moderately played": "GD",
    "good": "GD",
    "light played": "LP",
    "lightly played": "LP",
    "played": "PL",
    "heavily played": "PL",
    "poor": "PO",
    "damaged": "PO",
}
COND_ORDER = ["MT", "NM", "EX", "GD", "LP", "PL", "PO"]
COND_FACTOR = {"MT": 1.10, "NM": 1.00, "EX": 0.85, "GD": 0.70, "LP": 0.60, "PL": 0.45, "PO": 0.30}


class CardTraderError(Exception):
    pass


def configured() -> bool:
    return bool(TOKEN)


_last_call = 0.0
_rate_lock = asyncio.Lock()


async def _get(path: str, params: dict | None = None, throttle: bool = False, timeout: float = 40):
    """GET authentifié ; `throttle` respecte 1 appel/s ; 429 -> nouvel essai."""
    global _last_call
    if not TOKEN:
        raise CardTraderError("jeton CardTrader absent (variable CARDTRADER_TOKEN).")
    headers = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}
    last = None
    for attempt in range(3):
        if throttle:
            async with _rate_lock:
                wait = 1.1 - (time.monotonic() - _last_call)
                if wait > 0:
                    await asyncio.sleep(wait)
                _last_call = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
                r = await client.get(f"{BASE}{path}", params=params)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last = e
            await asyncio.sleep(1.5 * (attempt + 1))
            continue
        if r.status_code == 401:
            raise CardTraderError("jeton CardTrader refusé (401) : vérifie CARDTRADER_TOKEN.")
        if r.status_code == 404:
            return None
        if r.status_code in (429, 500, 502, 503, 504):
            last = Exception(f"HTTP {r.status_code}")
            await asyncio.sleep(1.5 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    raise CardTraderError(f"CardTrader indisponible ({last}).")


# ------------------------------------------------------------ correspondance ---

_STOP = {"the", "and", "of", "pokemon", "tcg"}


def _norm(s) -> str:
    s = str(s or "").lower().replace("é", "e").replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _tokens(s) -> set:
    return {t for t in _norm(s).split() if t not in _STOP}


def _set_score(a: str, b: str) -> float:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0
    if na == nb:
        return 10
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0
    jac = len(ta & tb) / len(ta | tb)
    score = jac * 9
    if ta == tb:
        score = 9.5
    elif ta <= tb or tb <= ta:
        score = max(score, 6.5)
    return score


def _num(s) -> str:
    """'030/030' -> '30' ; 'SV049' -> 'sv49' ; 'TG05' -> 'tg5'."""
    s = str(s or "").lower().split("/")[0].strip()
    m = re.match(r"^([a-z]*)0*(\d+)([a-z]*)$", s)
    return f"{m.group(1)}{m.group(2)}{m.group(3)}" if m else s


def pick_expansion(expansions: list[dict], game_id, set_name: str):
    """Renvoie (expansion, candidats) ; expansion=None si aucune assez proche."""
    scored = []
    for e in expansions:
        if game_id is not None and e.get("game_id") not in (None, game_id):
            continue
        scored.append((_set_score(set_name, e.get("name")), e))
    scored.sort(key=lambda x: -x[0])
    cands = [f'{e.get("name")} ({s:.1f})' for s, e in scored[:3]]
    if scored and scored[0][0] >= 6.5 and (len(scored) < 2 or scored[0][0] - scored[1][0] >= 1):
        return scored[0][1], cands
    return None, cands


def _bp_number(bp: dict) -> str:
    for src in ("fixed_properties", "properties", "editable_properties"):
        d = bp.get(src)
        if isinstance(d, dict):
            for k, v in d.items():
                if "number" in k.lower() and isinstance(v, (str, int)):
                    return _num(v)
    return ""


def pick_blueprint(blueprints: list[dict], name: str, number: str):
    """Même nom (ou nom commençant pareil) ET même numéro ; sans numéro lisible : nom unique seulement."""
    n = _norm(name)
    want = _num(number)
    same_name = []
    for b in blueprints:
        bn = _norm(b.get("name"))
        if bn == n or bn.startswith(n + " ") or n.startswith(bn + " "):
            same_name.append(b)
    if not same_name:
        return None
    with_num = [b for b in same_name if _bp_number(b) == want and want]
    if with_num:
        return with_num[0]
    if not any(_bp_number(b) for b in same_name) and len(same_name) == 1:
        return same_name[0]
    return None


async def _pokemon_game_id():
    gid = await store.get("ct:gameid")
    if gid:
        return gid
    games = await _get("/games") or []
    games = games.get("array", games) if isinstance(games, dict) else games
    for g in games:
        if "pok" in _norm(g.get("name")) or "pok" in _norm(g.get("display_name")):
            await store.set("ct:gameid", g["id"], STATIC_TTL)
            return g["id"]
    raise CardTraderError("jeu Pokémon introuvable chez CardTrader.")


async def find_blueprint(card: dict) -> dict:
    """Retrouve la fiche CardTrader d'une carte (mise en cache 30 jours)."""
    key = f"ct:bp:{card.get('id')}"
    hit = await store.get(key)
    if hit:
        return hit
    gid = await _pokemon_game_id()
    expansions = await store.get(f"ct:exp:{gid}")
    if not expansions:
        expansions = await _get("/expansions") or []
        expansions = expansions.get("array", expansions) if isinstance(expansions, dict) else expansions
        await store.set(f"ct:exp:{gid}", expansions, STATIC_TTL)
    exp, cands = pick_expansion(expansions, gid, card.get("setName") or "")
    if not exp:
        raise CardTraderError(f"série « {card.get('setName')} » introuvable chez CardTrader (proches : {', '.join(cands) or 'aucune'}).")
    bps = await _get("/blueprints/export", {"expansion_id": exp["id"]}, timeout=60) or []
    bps = bps.get("array", bps) if isinstance(bps, dict) else bps
    bp = pick_blueprint(bps, card.get("name"), card.get("number"))
    if not bp:
        raise CardTraderError(f"carte {card.get('name')} n°{card.get('number')} introuvable dans « {exp.get('name')} » chez CardTrader.")
    out = {"id": bp["id"], "name": bp.get("name"), "expansion": exp.get("name"),
           "cardMarketId": bp.get("card_market_id")}
    await store.set(key, out, STATIC_TTL)
    return out


# ---------------------------------------------------------------- annonces ---

def _price_eur(p: dict):
    pr = p.get("price")
    cents = cur = None
    if isinstance(pr, dict):
        cents, cur = pr.get("cents"), pr.get("currency") or pr.get("currency_iso")
    if cents is None:
        cents = p.get("price_cents")
        cur = cur or p.get("price_currency")
    if cents is None:
        return None, None
    cur = (cur or "EUR").upper()
    v = float(cents) / 100
    if cur == "EUR":
        return round(v, 2), cur
    if cur == "USD":
        return round(v * USD_EUR, 2), cur
    return None, cur


def _prop(props: dict, *needles):
    for k, v in (props or {}).items():
        if any(n in k.lower() for n in needles):
            return v
    return None


def build_prices(products: list[dict], lang: str) -> tuple[dict, list[str]]:
    """Annonces -> {cond: [≤5 plus bas prix]} pour la langue ; tolère les champs manquants."""
    wanted = set(LANG_CODES.get(lang, [lang.lower()]))
    buckets: dict[str, list[float]] = {}
    notes: list[str] = []
    converted = False
    for p in products or []:
        if p.get("graded") or (p.get("quantity") is not None and p.get("quantity") <= 0):
            continue
        props = p.get("properties_hash") or {}
        pl = _prop(props, "language")
        if pl is not None and str(pl).lower() not in wanted:
            continue
        cond = COND_MAP.get(_norm(_prop(props, "condition")))
        if not cond:
            continue
        price, cur = _price_eur(p)
        if price is None:
            continue
        if cur == "USD":
            converted = True
        buckets.setdefault(cond, []).append(price)
    if converted:
        notes.append("Certains prix CardTrader étaient en USD et ont été convertis en EUR (taux approximatif).")
    return {k: sorted(v)[:5] for k, v in buckets.items()}, notes


def complete_conditions(by_cond: dict) -> list[str]:
    """Complète les états sans annonce à partir de l'état le plus proche (coefficient). Renvoie les états estimés."""
    if not by_cond:
        return []
    estimated = []
    present = [c for c in COND_ORDER if c in by_cond]
    for c in COND_ORDER:
        if c in by_cond:
            continue
        j = min(present, key=lambda x: abs(COND_ORDER.index(x) - COND_ORDER.index(c)))
        by_cond[c] = [round(by_cond[j][0] * COND_FACTOR[c] / COND_FACTOR[j], 2)]
        estimated.append(c)
    # Un état moins bon ne peut pas coûter plus cher qu'un meilleur : on plafonne les états estimés.
    prev = None
    for c in COND_ORDER:
        if c in estimated and prev is not None and by_cond[c][0] > prev:
            by_cond[c] = [prev]
        prev = by_cond[c][0] if c in estimated else min(by_cond[c][0], prev) if prev is not None else by_cond[c][0]
    return estimated


async def get_cardtrader_prices(card: dict, lang: str) -> dict:
    """{'cardmarket': {lang: {cond: [prix…]}}, 'estimatedConds': [...], 'notes': [...], 'blueprint': {...}}"""
    bp = await find_blueprint(card)
    last_err = None
    products: list[dict] = []
    for code in LANG_CODES.get(lang, [lang.lower()]):
        data = await _get("/marketplace/products", {"blueprint_id": bp["id"], "language": code}, throttle=True)
        if isinstance(data, dict):
            products = data.get(str(bp["id"])) or data.get(bp["id"]) or next(iter(data.values()), []) or []
        elif isinstance(data, list):
            products = data
        if products:
            break
    by_cond, notes = build_prices(products, lang)
    if not by_cond:
        raise CardTraderError(f"aucune annonce {lang} chez CardTrader pour cette carte.")
    estimated = complete_conditions(by_cond)
    return {"cardmarket": {lang: by_cond}, "estimatedConds": estimated, "notes": notes, "blueprint": bp}


async def debug_cardtrader(card: dict, lang: str) -> dict:
    out: dict = {"configured": configured()}
    try:
        gid = await _pokemon_game_id()
        exps = await _get("/expansions") or []
        exps = exps.get("array", exps) if isinstance(exps, dict) else exps
        exp, cands = pick_expansion(exps, gid, card.get("setName") or "")
        out.update(gameId=gid, expansion=exp, expansionCandidates=cands)
        bp = await find_blueprint(card)
        out["blueprint"] = bp
        data = await _get("/marketplace/products", {"blueprint_id": bp["id"], "language": LANG_CODES.get(lang, [lang.lower()])[0]}, throttle=True)
        prods = data.get(str(bp["id"]), []) if isinstance(data, dict) else (data or [])
        out["productCount"] = len(prods)
        out["sampleProducts"] = prods[:3]
        out["parsed"] = build_prices(prods, lang)[0]
    except Exception as e:
        out["error"] = str(e) or type(e).__name__
    return out
