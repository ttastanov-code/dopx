import asyncio
import json
import aiohttp

BASE = "https://kffleague.kz/api/v1"

# матчей достаточно несколько для изучения структуры
MATCH_IDS = [898, 897, 896, 895, 894]


# =========================
# JSON schema builder
# =========================

def extract_schema(obj):

    if isinstance(obj, dict):
        return {k: extract_schema(v) for k, v in obj.items()}

    if isinstance(obj, list):
        if obj:
            return [extract_schema(obj[0])]
        return []

    return type(obj).__name__


# =========================
# download helper
# =========================

async def fetch(session, url):

    try:

        async with session.get(url) as resp:

            data = await resp.json()

            return data

    except:

        return None


# =========================
# collect match endpoints
# =========================

async def collect_match(session, match_id):

    endpoints = {

        "game": f"{BASE}/games/{match_id}?lang=kz",

        "lineup": f"{BASE}/games/{match_id}/lineup?lang=kz",

        "events": f"{BASE}/live/events/{match_id}?lang=kz",

        "stats": f"{BASE}/games/{match_id}/stats?lang=kz",

        "news": f"{BASE}/games/{match_id}/news?lang=kz&limit=10"
    }

    result = {}

    for name, url in endpoints.items():

        data = await fetch(session, url)

        result[name] = {
            "url": url,
            "data": data,
            "schema": extract_schema(data) if data else None
        }

    return match_id, result


# =========================
# main
# =========================

async def main():

    results = {}

    async with aiohttp.ClientSession() as session:

        tasks = []

        for mid in MATCH_IDS:
            tasks.append(collect_match(session, mid))

        responses = await asyncio.gather(*tasks)

        for mid, data in responses:
            results[mid] = data

    with open("kff_api_full_dump.json", "w", encoding="utf8") as f:

        json.dump(results, f, indent=2, ensure_ascii=False)

    print("DONE")
    print("matches:", len(results))


asyncio.run(main())