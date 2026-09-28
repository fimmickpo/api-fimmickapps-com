import io
import time
import asyncio
import uvicorn
from typing import List, Optional
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from PIL import Image

import ricacorp_scraper

try:
    from rembg import remove, new_session
    # 1. 預先載入模型 Session，全域共用，避免每次請求都重新載入
    rembg_session = new_session("u2net")
except ImportError:
    remove = None
    rembg_session = None

app = FastAPI(title="Image Processing API")

@app.get("/")
async def root():
    return Response(
        content="""<!DOCTYPE html>
<html lang="zh-Hant">
<head><meta charset="utf-8"><title>api.fimmickapps.com · Image Processing API</title>
<style>
  body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;max-width:720px;margin:40px auto;padding:0 20px;background:#f8f9fa;color:#222}
  h1{font-size:1.6rem;border-bottom:2px solid #dee2e6;padding-bottom:8px}
  .endpoint{background:#fff;border-radius:8px;padding:16px 20px;margin:12px 0;box-shadow:0 1px 3px rgba(0,0,0,.08)}
  .method{display:inline-block;font-weight:700;font-size:.85rem;padding:2px 8px;border-radius:4px;color:#fff}
  .POST{background:#2e7d32}
  .GET{background:#1565c0}
  .path{font-family:SFMono-Regular,Consolas,monospace;font-size:1.05rem;margin-left:10px}
  .desc{margin:8px 0 4px}
  .io{font-size:.9rem;color:#555;margin:2px 0}
  .io strong{color:#333}
  hr{border:none;border-top:1px solid #dee2e6;margin:24px 0}
</style>
</head><body>
<h1>🖼️ api.fimmickapps.com</h1>
<p>Image Processing API — 支援以下端點：</p>

<div class="endpoint">
  <span class="method POST">POST</span><span class="path">/img2webp</span>
  <div class="desc">將 JPG/PNG 轉換為 WebP 格式，自動縮放至寬度 ≤1280px</div>
  <div class="io"><strong>輸入：</strong><code>multipart/form-data</code> · <code>file</code>（image/jpeg 或 image/png）</div>
  <div class="io"><strong>輸出：</strong><code>image/webp</code> · Quality 85</div>
</div>

<div class="endpoint">
  <span class="method POST">POST</span><span class="path">/remove_bg</span>
  <div class="desc">使用 rembg (u2net) 移除圖像背景，保留透明 PNG</div>
  <div class="io"><strong>輸入：</strong><code>multipart/form-data</code> · <code>file</code>（image/jpeg 或 image/png）</div>
  <div class="io"><strong>輸出：</strong><code>image/png</code>（含透明通道）</div>
  <div class="io"><strong>備註：</strong>最多 2 個去背任務同時執行，其餘排隊等候</div>
</div>

<div class="endpoint">
  <span class="method POST">POST</span><span class="path">/rgb2cmyk</span>
  <div class="desc">將 RGB 圖像轉換為 CMYK 色彩模式，適合印刷用途</div>
  <div class="io"><strong>輸入：</strong><code>multipart/form-data</code> · <code>file</code>（image/jpeg 或 image/png）</div>
  <div class="io"><strong>輸出：</strong><code>image/jpeg</code> · CMYK 模式 · Quality 95</div>
  <div class="io"><strong>備註：</strong>RGBA 透明區域會以白色背景合成</div>
</div>

<div class="endpoint">
  <span class="method GET">GET</span><span class="path">/ricacorp_scraper</span>
  <div class="desc">爬取單一 Ricacorp 樓盤詳情頁，回傳樓盤資料 JSON</div>
  <div class="io"><strong>輸入：</strong>Query string <code>url</code>（樓盤詳情頁網址）</div>
  <div class="io"><strong>輸出：</strong><code>application/json</code> · 樓盤資料物件</div>
</div>

<div class="endpoint">
  <span class="method POST">POST</span><span class="path">/ricacorp_scraper</span>
  <div class="desc">批量爬取多個 Ricacorp 樓盤詳情頁</div>
  <div class="io"><strong>輸入：</strong><code>application/json</code> · <code>{"urls": [...], "delay": 1.0}</code></div>
  <div class="io"><strong>輸出：</strong><code>application/json</code> · <code>{"results": [...]}</code></div>
</div>

<hr>
<p style="font-size:.85rem;color:#888">以 <code>curl -F file=@photo.jpg https://api.fimmickapps.com/rgb2cmyk -o output.jpg</code> 呼叫</p>
</body></html>""",
        media_type="text/html",
        headers={"Content-Type": "text/html; charset=utf-8"}
    )

# 2. 設定併發限制 (Semaphore)
# 限制同時間最多只能有 2 個去背任務執行 (可根據你的伺服器 RAM 大小調整)
bg_removal_semaphore = asyncio.Semaphore(2)

# 把 Pillow 的 CPU 密集操作獨立成一般函數
def process_webp_conversion(content: bytes) -> bytes:
    img = Image.open(io.BytesIO(content))
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGBA")
    else:
        img = img.convert("RGB")
        
    img.thumbnail((1280, img.height))
    out_io = io.BytesIO()
    img.save(out_io, format="WEBP", quality=85)
    return out_io.getvalue()

def process_cmyk_conversion(content: bytes) -> bytes:
    img = Image.open(io.BytesIO(content))
    # RGBA → 先以白色背景合成再轉 CMYK，避免透明區域產生奇怪顏色
    if img.mode == "RGBA":
        background = Image.new("RGB", img.size, (255, 255, 255))
        background.paste(img, mask=img.split()[3])
        img = background
    elif img.mode != "RGB":
        img = img.convert("RGB")
    cmyk_img = img.convert("CMYK")
    out_io = io.BytesIO()
    cmyk_img.save(out_io, format="JPEG", quality=95)
    return out_io.getvalue()

@app.post("/img2webp")
async def convert_image(file: UploadFile = File(...)):
    if file.content_type not in ["image/jpeg", "image/png"]:
        raise HTTPException(status_code=400, detail="只接受 JPG 或 PNG")
    try:
        content = await file.read()
        # 3. 使用 to_thread 把 CPU 運算丟到背景執行緒，不阻塞其他請求
        webp_bytes = await asyncio.to_thread(process_webp_conversion, content)
        return Response(content=webp_bytes, media_type="image/webp")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/remove_bg")
async def remove_background(file: UploadFile = File(...)):
    if remove is None:
        raise HTTPException(status_code=500, detail="未安裝 rembg")
    if file.content_type not in ["image/jpeg", "image/png"]:
        raise HTTPException(status_code=400, detail="只接受 JPG 或 PNG")
        
    try:
        content = await file.read()
        
        # 4. 透過 Semaphore 控管並發數量，超出的 Request 會在這裡乖乖排隊
        async with bg_removal_semaphore:
            # 同樣使用 to_thread，並傳入我們預先載入的 session
            result_bytes = await asyncio.to_thread(
                remove, 
                data=content, 
                session=rembg_session
            )
            
        return Response(content=result_bytes, media_type="image/png")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/rgb2cmyk")
async def convert_to_cmyk(file: UploadFile = File(...)):
    if file.content_type not in ["image/jpeg", "image/png"]:
        raise HTTPException(status_code=400, detail="只接受 JPG 或 PNG")
    try:
        content = await file.read()
        cmyk_bytes = await asyncio.to_thread(process_cmyk_conversion, content)
        return Response(content=cmyk_bytes, media_type="image/jpeg")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class ScrapeRequest(BaseModel):
    urls: List[str]
    delay: float = 1.0

def _scrape_one(url: str) -> dict:
    try:
        html = ricacorp_scraper.fetch(url)
    except (ricacorp_scraper.HTTPError, ricacorp_scraper.URLError) as e:
        return {"url": url, "error": str(e)}
    return ricacorp_scraper.parse_detail(html, url)

def _scrape_many(urls: List[str], delay: float) -> List[dict]:
    records = []
    for i, url in enumerate(urls):
        records.append(_scrape_one(url))
        if delay and i < len(urls) - 1:
            time.sleep(delay)
    return records

@app.get("/ricacorp_scraper")
async def ricacorp_scrape_get(url: str):
    try:
        record = await asyncio.to_thread(_scrape_one, url)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return record

@app.post("/ricacorp_scraper")
async def ricacorp_scrape_post(req: ScrapeRequest):
    if not req.urls:
        raise HTTPException(status_code=400, detail="請提供至少一個 URL")
    try:
        records = await asyncio.to_thread(_scrape_many, req.urls, req.delay)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"results": records}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)
