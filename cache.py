"""
Cache des données (prix, fiches de cartes, liens Cardmarket).

- Toujours : cache MÉMOIRE (rapide, mais perdu à chaque redémarrage du serveur —
  le plan gratuit de Render s'endort après ~15 min d'inactivité et efface sa mémoire).
- Si UPSTASH_REDIS_REST_URL et UPSTASH_REDIS_REST_TOKEN sont définies : cache
  DURABLE partagé (Upstash Redis, offre gratuite : 10 000 commandes/jour, 256 Mo).
  Les données survivent aux redémarrages et aux mises à jour du serveur.
"""
import os
import time
import json
import logging
import httpx

log = logging.getLogger("cardprix.cache")

_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
_L1: dict[str, tuple[float, object]] = {}
_L1_PERSISTENT_TTL = 300   # avec Redis, la mémoire locale ne sert que de tampon de 5 min


def persistent() -> bool:
    return bool(_URL and _TOKEN)


async def _redis(cmd: list):
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            r = await c.post(_URL, json=cmd, headers={"Authorization": f"Bearer {_TOKEN}"})
        data = r.json()
        if "error" in data:
            log.warning("redis %s: %s", cmd[0], data["error"])
            return None
        return data.get("result")
    except Exception as e:
        log.warning("redis %s indisponible: %s", cmd[0], e)
        return None


async def get(key: str):
    now = time.time()
    hit = _L1.get(key)
    if hit and hit[0] > now:
        return hit[1]
    if persistent():
        raw = await _redis(["GET", key])
        if raw:
            try:
                value = json.loads(raw)
                _L1[key] = (now + _L1_PERSISTENT_TTL, value)
                return value
            except ValueError:
                pass
    return None


async def set(key: str, value, ttl: int):
    ttl = int(ttl)
    _L1[key] = (time.time() + (min(ttl, _L1_PERSISTENT_TTL) if persistent() else ttl), value)
    if persistent():
        await _redis(["SET", key, json.dumps(value, ensure_ascii=False), "EX", ttl])
