#!/usr/bin/env python3
"""
Ricacorp 樓盤詳情頁 scraper
==========================
爬取 https://www.ricacorp.com/zh-hk/property/detail/... 嘅二手樓盤詳情頁。

詳情頁 HTML 入面有隻 Angular Universal 嘅 <script id="serverApp-state"> 標籤，
包含成個樓盤嘅 JSON 資料 (POST object)，唔使 render JS 都攞得晒大部份欄位。
淨係「屋苑賣點」呢段文字冇喺詳情頁度，要另外攞返個屋苑頁
(https://www.ricacorp.com/zh-hk/property/estate/{estateAliasV4}) 先有。

用法:
    python3 ricacorp_scraper.py URL [URL ...]
    python3 ricacorp_scraper.py -i urls.txt          # 每行一個 URL
    python3 ricacorp_scraper.py URL -o out.csv        # 自訂輸出檔名 (CSV/JSON)

輸出: 預設 ricacorp_results.csv (+ .json)
"""

import argparse, csv, json, re, sys, time
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

ESTATE_URL = "https://www.ricacorp.com/zh-hk/property/estate/{}"

# 二手 = 3 (出售) / 5 (出租)；一手都摞埋以防萬一 (JSON 入面 agreementType 係字串)
AGREEMENT_TYPE_LABEL = {"1": "出售", "2": "出租", "3": "出售", "4": "出租", "5": "出租"}

# ---------------------------------------------------------------- fetch ---
def fetch(url: str, timeout: int = 30) -> str:
    req = Request(url, headers={"User-Agent": UA, "Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")

# ------------------------------------------------------------- state json -
def _unescape_angular(s: str) -> str:
    """Angular TransferState 用嘅簡易 escape (見 @angular/platform-browser)。
    Encode 次序係 & -> &a; 行先，所以 decode 一定要留返 &a; 喺最尾先做。"""
    return (s.replace("&q;", '"').replace("&s;", "'")
             .replace("&l;", "<").replace("&g;", ">").replace("&a;", "&"))

def extract_post(html: str) -> dict:
    """喺詳情頁 HTML 攞返 serverApp-state 入面嘅 POST object (樓盤全部資料)。"""
    m = re.search(r'<script id="serverApp-state" type="application/json">(.*?)</script>', html, re.S)
    if not m:
        return {}
    try:
        state = json.loads(_unescape_angular(m.group(1)))
    except json.JSONDecodeError:
        return {}
    return state.get("POST", {}) or {}

# -------------------------------------------------------------- helpers ---
def _extract_age(html: str):
    """樓齡: 淨係 serverApp-state 冇呢個欄位，要喺 render 好嘅 HTML 度攞。"""
    m = re.search(r'class="item-description">樓齡</(?:div|span)>.*?'
                  r'class="ng-star-inserted">(\d+)<span class="years">年', html, re.S)
    return f"{m.group(1)}年" if m else None

def _format_price(price, agreement_label: str):
    if price in (None, ""):
        return None
    if agreement_label == "出租":
        return f"${price:,.0f}"
    return f"${price / 10000:,.0f}萬"

def _format_efficiency(ratio):
    if not ratio:
        return "-"
    return f"{round(ratio)}%"

def _format_rooms(post: dict):
    room = post.get("room") or 0
    hall = post.get("hall") or 0
    washroom = post.get("washroom") or 0
    suite = post.get("suite") or 0
    maid = post.get("maidRoom") or 0
    s = f"{room}房"
    if suite:
        s += f"({suite}套)"
    if hall:
        s += f"{hall}廳"
    if maid:
        s += f"{maid}工人房"
    if washroom:
        s += f"{washroom}廁"
    return s

def _format_school_net(post: dict):
    primary = post.get("schoolNet")
    if not primary:
        return None
    secondary = None
    for tag in post.get("locationTags") or []:
        sm = re.match(r"^中學(.+)$", tag)
        if sm:
            secondary = sm.group(1)
            break
    return f"小學:{primary} / 中學:{secondary}" if secondary else f"小學:{primary}"

def _estate_name_and_district(post: dict):
    """屋苑名稱同區域: 兩者都可以由 publicLocationNamesHma / AliasesHma 呢兩條
    平行陣列度攞 (跟 estateAliasV4 對得上嗰個位置), 會過濾埋 phase/座數等尾巴。"""
    names = post.get("publicLocationNamesHma") or []
    aliases = post.get("publicLocationAliasesHma") or []
    estate_alias = post.get("estateAliasV4")
    if estate_alias and estate_alias in aliases:
        idx = aliases.index(estate_alias)
        estate_name = names[idx] if idx < len(names) else post.get("displayText")
        district = names[idx - 1] if idx > 0 else None
        return estate_name, district
    return post.get("displayText"), post.get("publicBusinessRegionText")

_estate_blurb_cache: dict = {}

def fetch_estate_highlight(estate_alias: str, delay: float = 0.0):
    """屋苑賣點: 攞屋苑頁第一段 seo-message (通常係屋苑概覽/賣點簡介)。"""
    if not estate_alias:
        return None
    if estate_alias in _estate_blurb_cache:
        return _estate_blurb_cache[estate_alias]
    url = ESTATE_URL.format(quote(estate_alias, safe=""))
    try:
        html = fetch(url)
    except (HTTPError, URLError):
        _estate_blurb_cache[estate_alias] = None
        return None
    if delay:
        time.sleep(delay)
    m = re.search(r'class="seo-message[^"]*">([^<]*)</span>', html)
    text = m.group(1).strip() if m else None
    _estate_blurb_cache[estate_alias] = text
    return text

# -------------------------------------------------------------- parsing ---
def parse_detail(html: str, url: str, delay: float = 0.0) -> dict:
    post = extract_post(html)
    if not post:
        return {"url": url, "error": "找唔到 serverApp-state 嘅 POST 資料"}

    agreement_label = AGREEMENT_TYPE_LABEL.get(str(post.get("agreementType")), None)
    estate_name, district = _estate_name_and_district(post)

    rec = {
        "url": url,
        "樓盤名稱": estate_name,
        "交易類型": agreement_label,
        "價格": _format_price(post.get("price"), agreement_label),
        "實用面積": f"{post.get('saleableArea')}呎" if post.get("saleableArea") else None,
        "物業編號": post.get("postNo"),
        "區域": district,
        "屋苑賣點": fetch_estate_highlight(post.get("estateAliasV4"), delay=delay),
        "校網": _format_school_net(post),
        "座室樓層": " ".join(x for x in [post.get("floorZoneText"), post.get("flatZoneText")] if x) or None,
        "房數": _format_rooms(post),
        "實用率": _format_efficiency(post.get("efficiencyRatio")),
        "樓齡": _extract_age(html),
    }
    return rec

# ---------------------------------------------------------------- main ----
def main():
    ap = argparse.ArgumentParser(description="Ricacorp 樓盤詳情頁 scraper")
    ap.add_argument("urls", nargs="*", help="樓盤詳情頁 URL (可以有多個)")
    ap.add_argument("-i", "--input", help="包含 URL 嘅文字檔 (每行一個)")
    ap.add_argument("-o", "--output", default="ricacorp_results.csv",
                    help="輸出檔名 (預設 ricacorp_results.csv)")
    ap.add_argument("--delay", type=float, default=1.0, help="每個請求之間 delay 秒數 (預設 1.0)")
    args = ap.parse_args()

    urls = list(args.urls)
    if args.input:
        with open(args.input, encoding="utf-8") as f:
            urls.extend(line.strip() for line in f if line.strip())
    if not urls:
        ap.error("請提供至少一個樓盤詳情頁 URL (positional 或 -i)")

    records = []
    for i, url in enumerate(urls, 1):
        print(f"[{i}/{len(urls)}] {url}")
        try:
            html = fetch(url)
        except (HTTPError, URLError) as e:
            print(f"  [!] 攞唔到頁面: {e}")
            records.append({"url": url, "error": str(e)})
            continue
        rec = parse_detail(html, url, delay=args.delay)
        records.append(rec)
        if rec.get("error"):
            print(f"  [!] {rec['error']}")
        else:
            print(f"  {rec['樓盤名稱']} | {rec['交易類型']} {rec['價格']} | {rec['區域']}")
        time.sleep(args.delay)

    cols = ["url", "樓盤名稱", "交易類型", "價格", "實用面積", "物業編號", "區域",
            "屋苑賣點", "校網", "座室樓層", "房數", "實用率", "樓齡", "error"]
    with open(args.output, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in records:
            w.writerow({c: r.get(c, "") for c in cols})
    json_name = args.output.rsplit(".", 1)[0] + ".json"
    with open(json_name, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=1)
    print(f"已輸出: {args.output} (+ {json_name})")

if __name__ == "__main__":
    main()
