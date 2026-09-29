#!/usr/bin/env python3
"""Search public Ricacorp buy/rent listings (ported from data-collect/ricacorp/ricacorp_search.py)."""

import json
import os
import re
import sys
import threading
import time
from functools import lru_cache
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.parse import quote
from urllib.request import Request, urlopen

import ricacorp_scraper


BASE = "https://www.ricacorp.com/zh-hk/property"
API = f"{BASE}/api/post"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; RicacorpPublicSearch/1.0)",
    "x-custom-header": "www.ricacorp.com/zh-hk/property",
    "Accept": "application/json",
}
REGIONS = {
    "港島": "e1e4eefe-f6e0-476d-9fd4-60df525853c3",
    "九龍": "90ac8e34-a8fa-4198-a0ed-e93be7a1e174",
    "新界東": "2f9936d3-132c-4de9-8882-3d56fa86c806",
    "新界西": "4b727ba9-f2cb-4ee3-9b9c-9270c8e193f9",
}
FILTER_TAGS = {
    "$100印花稅精選": "100_ssd", "八成按揭": "80_ltv_2023", "九成按揭": "90_ltv_2023",
    "寵物樂園": "寵物樂園", "星級盤": "星級盤", "KOL": "kol", "VR/AI裝修": "vr360",
    "影片": "rcvideo_alicloud", "室內相": "indoor", "筍盤": "筍", "有匙": "有匙",
    "連車位": "連車位", "減價盤": "減價盤", "半新樓": "半新樓", "獨家": "獨家",
    "基本裝修": "基本裝修", "豪華裝修": "豪華裝修", "品味裝修": "品味裝修",
    "露台": "露台", "平台": "平台", "天台": "天台", "花園": "花園",
    "空中花園": "空中花園", "私人泳池": "私人泳池", "車房": "車房", "工人房": "工人房",
    "工人套廁": "工人套廁", "內置樓梯": "內置樓梯", "外置樓梯": "外置樓梯",
    "開放式廚房": "開放式廚房", "近港鐵": "近港鐵", "會所設施": "會所設施",
    "屋苑專車": "屋苑專車", "近大型商場": "近大型商場", "市景": "市景", "樓景": "樓景",
    "海景": "海景", "維港景": "維港景", "山景": "山景", "園景": "園景", "池景": "池景",
    "河景": "河景", "馬場景": "馬場景", "開揚景": "開揚景", "會所景": "會所景",
    "開放式單位": "開放式單位", "複式單位": "複式單位", "三層複式": "三層複式",
    "相連單位": "相連單位", "頂層單位": "頂層單位", "豪宅": "豪宅", "洋房": "洋房",
    "連租約": "連租約", "私樓": "私人住宅", "車位": "車位", "銀主盤": "銀主盤",
    "居屋(已補)": "居屋（已補地價）", "居屋(未補)": "居屋（未補地價）",
    "公屋(已補)": "租置屋（已補地價）", "公屋(未補)": "租置屋（未補地價）",
}
ALIASES = {"近地鐵": "近港鐵"}  # colloquial term -> Ricacorp tag label
CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "ricacorp_cache.json")
CACHE_TTL = {"tags": 86400, "suggest": 30 * 86400}  # seconds; tags change more often than location names
CACHE_LOCK = threading.Lock()  # FastAPI runs searches in worker threads


def parse_query(query, limit=5, tags=None, suggest=None):
    """Parse transaction, area, room, price, tag, and location terms in any order.

    `suggest(text)` returns Ricacorp location suggestions; None keeps the parser offline.
    """
    tags = tags or FILTER_TAGS
    for alias, target in ALIASES.items():
        query = query.replace(alias, target)
    rental_query = query.replace("連租約", "").replace("租置屋", "")
    is_rent = bool(re.search(r"租樓|租盤|租屋|出租|月租|想租|求租|^\s*租|\brent(?:al)?\b", rental_query, re.I))
    params = {"agreementType": "5" if is_rent else "3", "language": "HK", "limit": str(limit), "offset": "0"}
    rest = query
    rest = re.sub(r"全港|想買|求購|買樓|買盤|買屋|出售|二手樓|租樓|租盤|租屋|出租|月租|想租|求租", " ", rest)
    rest = re.sub(r"^\s*租(?=\S)|(?<=\s)租(?=\s|$)", " ", rest)

    area = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*(?:sq\s*ft|sqft|ft²?|呎|平方呎)\s*(以下|以內|以上|起)?", rest, re.I)
    if area:
        value = area.group(1).replace(",", "")
        (params.__setitem__("saleableAreaTo" if area.group(2) in ("以下", "以內") else "saleableAreaFrom", value))
        rest = rest[:area.start()] + " " + rest[area.end():]

    room = re.search(r"(一|二|兩|三|四|五|六|[1-6])\s*房(?:間)?\s*(以下|以內|以上|起)?", rest)
    if room:
        number = int(room.group(1)) if room.group(1).isdigit() else {"一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6}[room.group(1)]
        params["roomTo" if room.group(2) in ("以下", "以內") else "roomFrom"] = str(number)
        if not room.group(2):
            params["roomTo"] = str(number)
        rest = rest[:room.start()] + " " + rest[room.end():]

    price = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*(million|萬|万|m|k|千)\s*(以下|以內|以上|起)?", rest, re.I)
    if price:
        amount = float(price.group(1).replace(",", ""))
        unit = price.group(2).lower()
        # In this Chinese property-search context, 1000m means 1,000 萬;
        # smaller M values use the common English million shorthand (e.g. 10m).
        multiplier = 1_000 if unit in ("k", "千") else (10_000 if unit in ("萬", "万") or (unit == "m" and amount >= 100) else 1_000_000)
        hkd = round(amount * multiplier)
        params["priceFrom" if price.group(3) in ("以上", "起") else "priceTo"] = str(hkd)
        rest = rest[:price.start()] + " " + rest[price.end():]

    # Regions first, so 新界東 is not split by the location scan; 九龍塘/西九龍 are districts, not the region.
    for name, aliases in (("港島", r"港島區?|香港島|hong\s*kong\s*island"), ("九龍", r"(?<!西)九龍(?![塘站灣城])區?"), ("新界東", r"新界東"), ("新界西", r"新界西")):
        if re.search(aliases, rest, re.I):
            params["locationId"] = REGIONS[name]  # a later estate/district match is more specific and overrides it
            rest = re.sub(aliases, " ", rest, flags=re.I)
            break
    matched_tags = []
    rest = match_tags(rest, tags, matched_tags)
    if suggest:
        rest = match_locations(rest, suggest, tags, params)
        rest = match_tags(rest, tags, matched_tags)
    if matched_tags:
        params["postTags"] = ",".join(dict.fromkeys(matched_tags))

    keyword = " ".join(rest.split())
    if keyword:
        params["displayText"] = keyword
    return params


def match_tags(rest, tags, matched):
    for label in sorted(tags, key=len, reverse=True):
        pattern = rf"(?<!\w){re.escape(label)}(?!\w)"
        if re.search(pattern, rest):
            matched.append(tags[label])
            rest = re.sub(pattern, " ", rest)
    return rest


def match_locations(rest, suggest, tags, params):
    """Split unspaced text into estate/district names and tag labels, longest match first."""
    out, best_depth = [], -1

    def use(loc):
        nonlocal best_depth
        if len(loc["pathNames"]) > best_depth:  # ponytail: one locationId; keep the most specific match
            params["locationId"], best_depth = loc["locationId"], len(loc["pathNames"])

    is_word = lambda text: bool(re.fullmatch(r"[\u4e00-\u9fff]{2,}", text))
    for chunk in rest.split():
        whole = suggest(chunk) if is_word(chunk) and chunk not in tags else []
        exact = [loc for loc in whole if chunk in {loc["displayText"], *loc["displayText"].split("/")}]
        if exact:  # e.g. 康怡花園, before its 花園 can be read as a tag
            use(max(exact, key=lambda loc: len(loc["pathNames"])))
            continue
        i, free, pieces = 0, "", []
        while i < len(chunk):
            found = [(label, None) for label in tags if chunk.startswith(label, i)]
            if chunk not in tags and is_word(chunk[i:i + 2]):
                found += [(part, r) for r in suggest(chunk[i:i + 2]) for part in {r["displayText"], *r["displayText"].split("/")} if chunk.startswith(part, i)]
            if not found:
                free, i = free + chunk[i], i + 1
                continue
            name, loc = max(found, key=lambda n: (len(n[0]), len(n[1]["pathNames"]) if n[1] else 0))
            if loc:
                use(loc)
            pieces += [free, name if not loc else ""]
            free, i = "", i + len(name)
        if not pieces and whole and len(whole[0]["pathNames"]) == 4:
            use(whole[0])  # Ricacorp alias, e.g. 深水埗 -> 西九龍, 上環 -> 中上環/西區
            continue
        out += pieces + [free]
    return " ".join(out)


def load_cache():
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


CACHE = None


def cached(kind, key, fetch):
    """File cache so repeated runs do not hit Ricacorp again; failures are not cached."""
    global CACHE
    with CACHE_LOCK:
        if CACHE is None:
            CACHE = load_cache()
        entry = CACHE.setdefault(kind, {}).get(key)
    if entry and time.time() - entry["at"] < CACHE_TTL[kind]:
        return entry["data"]
    try:
        data = fetch()
    except (HTTPError, URLError, TimeoutError, ValueError) as error:
        print(f"Ricacorp {kind} lookup failed for {key!r}: {error}", file=sys.stderr)
        return entry["data"] if entry else None
    with CACHE_LOCK:
        CACHE[kind][key] = {"at": time.time(), "data": data}
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        tmp = f"{CACHE_PATH}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(CACHE, f, ensure_ascii=False)
        os.replace(tmp, CACHE_PATH)  # atomic, so a crash never leaves a half-written cache
    time.sleep(0.3)
    return data


def suggest_locations(text):
    def fetch():
        query = urlencode({"searchText": text, "fields": "results(locationId,displayText,pathNames)"})
        with urlopen(Request(f"{BASE}/api/location/v3/suggest?{query}", headers=HEADERS), timeout=30) as response:
            return json.load(response).get("results", [])
    return cached("suggest", text, fetch) or []


@lru_cache(maxsize=None)  # one fetch attempt per run, even when it fails
def load_tags():
    """Ricacorp's current tag labels from the list page, falling back to FILTER_TAGS."""
    def fetch():
        state = extract_state(ricacorp_scraper.fetch(f"{BASE}/list/buy", timeout=90))
        found = {}
        def walk(node):
            if isinstance(node, dict):
                label = (node.get("displayTexts") or {}).get("HK") if "postTagId" in node else None
                if label and node.get("key"):
                    found[label] = node["key"]
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)
        walk(state)
        if not found:
            raise ValueError("no tags in list page")
        return found
    return {**FILTER_TAGS, **(cached("tags", "list", fetch) or {})}


def extract_state(html):
    match = re.search(r'<script id="serverApp-state" type="application/json">(.*?)</script>', html, re.S)
    try:
        return json.loads(ricacorp_scraper._unescape_angular(match.group(1))) if match else {}
    except json.JSONDecodeError:
        return {}


def fetch_listings(params):
    request = Request(f"{API}?{urlencode(params)}", headers=HEADERS)
    with urlopen(request, timeout=45) as response:
        payload = json.load(response)
    if payload.get("total") == 0 and "results" not in payload:
        payload["results"] = []
    if not isinstance(payload.get("results"), list):
        raise ValueError(f"Unexpected Ricacorp response: {payload}")
    return payload


def listing_row(post, rank, query, include_details=True):
    alias = post.get("aliasV4") or post.get("alias") or ""
    price = post.get("marketPrice") or 0
    area = post.get("saleableArea") or 0
    url = f"{BASE}/detail/{quote(alias, safe='')}" if alias else ""
    details = {}
    if url and include_details:
        try:
            details = ricacorp_scraper.parse_detail(ricacorp_scraper.fetch(url), url)
        except (ricacorp_scraper.HTTPError, ricacorp_scraper.URLError, TimeoutError) as error:
            details = {"error": str(error)}
    return {
        "rank": rank,
        "query": query,
        "transaction": "租盤" if post.get("agreementType") == "5" else "買盤",
        "property": details.get("樓盤名稱") or post.get("displayTextHk") or post.get("displayText") or "",
        "district": post.get("publicBusinessRegionText") or "",
        "layout": " ".join(x for x in (post.get("room") and f'{post["room"]}房', post.get("floorZoneText"), post.get("flatZoneText")) if x),
        "saleable_area_sqft": area,
        "price_hkd": price,
        "price_per_sqft_hkd": round(price / area) if area else "",
        "post_no": post.get("postNo") or "",
        "url": url,
        **details,
    }


def search(query, limit=5, include_details=True, delay=0.5):
    params = parse_query(query, limit, load_tags(), suggest_locations)
    payload = fetch_listings(params)
    rows = []
    for i, post in enumerate(payload["results"][:limit], 1):
        rows.append(listing_row(post, i, query, include_details))
        if include_details and delay:
            time.sleep(delay)  # ponytail: polite fixed delay per detail page
    return {"query": query, "params": params, "total": payload.get("total", 0), "results": rows}


def self_check():
    p = parse_query("港島區 400ft 1000m以下")
    assert p["locationId"] == REGIONS["港島"]
    assert p["saleableAreaFrom"] == "400"
    assert p["priceTo"] == "10000000"
    assert parse_query("400呎以下 800萬以上")["saleableAreaTo"] == "400"
    assert parse_query("租樓 港島 400ft 20k以下")["agreementType"] == "5"
    assert parse_query("租樓 港島 400ft 20k以下")["priceTo"] == "20000"
    p = parse_query("太古城 海景 兩房 400呎 1000萬以下")
    assert p["displayText"] == "太古城" and p["postTags"] == "海景" and p["roomFrom"] == p["roomTo"] == "2"
    assert parse_query("租北角 兩房")["agreementType"] == "5"
    p = parse_query("想租太古城 有匙 露台 400呎 20k以下")
    assert p["agreementType"] == "5" and p["postTags"] == "有匙,露台" and p["displayText"] == "太古城"
    assert parse_query("連租約 兩房")["agreementType"] == "3"
    p = parse_query("康怡花園 400ft 1000m以下")
    assert p["displayText"] == "康怡花園" and "postTags" not in p
    places = {"將軍": [{"locationId": "tko", "displayText": "將軍澳", "pathNames": ["利嘉閣", "住宅", "九龍", "將軍澳"]}],
              "觀塘": [{"locationId": "kt", "displayText": "觀塘/藍田/油塘", "pathNames": ["利嘉閣", "住宅", "九龍", "觀塘/藍田/油塘"]}],
              "康怡": [{"locationId": "kf", "displayText": "康怡花園", "pathNames": ["利嘉閣", "住宅", "香港島", "康怡", "康怡花園"]}]}
    fake = lambda text: places.get(text, [])
    p = parse_query("將軍澳二手樓", suggest=fake)
    assert p["agreementType"] == "3" and p["locationId"] == "tko" and "displayText" not in p
    p = parse_query("觀塘一房開廁", suggest=fake)
    assert p["locationId"] == "kt" and p["roomTo"] == "1" and p["displayText"] == "開廁"
    p = parse_query("康怡花園海景近地鐵 港島", suggest=fake)
    assert p["locationId"] == "kf" and set(p["postTags"].split(",")) == {"海景", "近港鐵"} and "displayText" not in p


if __name__ == "__main__":
    self_check()
    print("Parser check passed.")
