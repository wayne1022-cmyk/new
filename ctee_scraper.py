"""
CTEE 盤前新聞爬蟲（GitHub Actions / 本機通用版）

流程：
1. 進入工商時報即時新聞 → 找最新「盤前｜」Hub 文章
2. 進入 Hub，取出「利多因子速覽」到「將工商時報加入Google偏好來源」之間的文章連結
3. 輸出 output/premarket_articles.json，供後續 AI 分析、寄信程式使用
"""

import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin

from pyppeteer import launch

# ============================================================
# 1. 基本設定
# ============================================================

BASE_URL = "https://www.ctee.com.tw"
LIVE_NEWS_URL = "https://www.ctee.com.tw/livenews/stock"

START_MARKER = "利多因子速覽"
END_MARKER = "將工商時報加入Google偏好來源"

OUTPUT_DIR = "output"
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "premarket_articles.json")

TW_TZ = timezone(timedelta(hours=8))

# 預設使用「有畫面」的 Chrome（在 GitHub 上搭配 xvfb 虛擬螢幕），
# 比 headless 更不容易被 Cloudflare 判定為機器人。要用 headless 可設 HEADLESS=true
HEADLESS = os.environ.get("HEADLESS", "false").lower() == "true"

# 是否允許在「今天的盤前還沒發布」時，改用最近一篇（測試用）
ALLOW_STALE = os.environ.get("ALLOW_STALE", "false").lower() == "true"

# 今天的盤前還沒出現時，最多等待幾分鐘（每 5 分鐘重新檢查一次）
MAX_WAIT_MINUTES = int(os.environ.get("MAX_WAIT_MINUTES", "0") or 0)

# 依序尋找 Chrome：環境變數 > Linux (GitHub Actions) > Mac
CHROME_CANDIDATES = [
    os.environ.get("CHROME_PATH"),
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def find_chrome():
    for path in CHROME_CANDIDATES:
        if path and os.path.exists(path):
            return path
    raise RuntimeError("找不到 Chrome，請設定環境變數 CHROME_PATH")


# ============================================================
# 2. 啟動 Chrome（headless）
# ============================================================

async def create_browser():
    chrome_path = find_chrome()
    print(f"使用 Chrome：{chrome_path}")

    return await launch(
        headless=HEADLESS,
        ignoreDefaultArgs=["--enable-automation"],
        executablePath=chrome_path,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-blink-features=AutomationControlled",
            "--window-size=1920,1080",
            "--lang=zh-TW",
        ],
        handleSIGINT=False,
        handleSIGTERM=False,
        handleSIGHUP=False,
    )


class BlockedError(RuntimeError):
    """被 Cloudflare 驗證頁擋下。"""


CHALLENGE_TITLES = ("請稍候", "Just a moment", "Attention Required")
CHALLENGE_TEXTS = ("正在執行安全驗證", "Checking your browser", "Verify you are human")


async def is_challenge_page(page):
    try:
        title = await page.title()
        body = await page.evaluate("() => (document.body ? document.body.innerText : '').slice(0, 500)")
    except Exception:
        return True  # 頁面正在跳轉中，視為尚未完成
    return any(t in title for t in CHALLENGE_TITLES) or any(t in body for t in CHALLENGE_TEXTS)


async def wait_for_challenge(page, timeout=60):
    """遇到 Cloudflare 驗證頁時等待它自動通過。回傳是否通過。"""
    if not await is_challenge_page(page):
        return True

    print("⚠️ 偵測到 Cloudflare 安全驗證，等待自動通過…")
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        await asyncio.sleep(2)
        if not await is_challenge_page(page):
            print(f"✅ 驗證通過（等了 {time.monotonic() - start:.0f} 秒）")
            await asyncio.sleep(2)
            return True

    print("❌ 驗證逾時，仍停在 Cloudflare 驗證頁")
    return False


async def safe_goto(page, url):
    """networkidle2 在廣告多的網站可能逾時，失敗就退而求其次；並處理 Cloudflare 驗證頁。"""
    try:
        await page.goto(url, {"waitUntil": "networkidle2", "timeout": 60000})
    except Exception as e:
        print(f"networkidle2 逾時（{e}），改用 domcontentloaded 重試")
        await page.goto(url, {"waitUntil": "domcontentloaded", "timeout": 60000})
    await asyncio.sleep(3)

    if not await wait_for_challenge(page):
        await dump_debug(page, "blocked")
        raise BlockedError(f"被 Cloudflare 擋下：{url}")


# ============================================================
# 2.5 除錯 / 反偵測工具
# ============================================================

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
"""


async def prepare_page(page, browser):
    """基本設定。有畫面模式不改 UA（和真實環境一致）；headless 才去掉 HeadlessChrome 字樣。"""
    version = await browser.version()
    print(f"瀏覽器版本：{version}")

    if HEADLESS:
        m = re.search(r"/(\d+\.\d+\.\d+\.\d+)", version)
        chrome_ver = m.group(1) if m else "131.0.0.0"
        await page.setUserAgent(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{chrome_ver} Safari/537.36"
        )

    await page.setViewport({"width": 1920, "height": 1080})
    await page.setExtraHTTPHeaders({"Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8"})
    await page.evaluateOnNewDocument(STEALTH_JS)


async def dump_debug(page, name):
    """印出頁面狀態，並存下截圖與 HTML（會隨 artifact 一起上傳）。"""
    try:
        os.makedirs(os.path.join(OUTPUT_DIR, "debug"), exist_ok=True)

        info = await page.evaluate(
            """
            () => ({
                url: location.href,
                title: document.title,
                linkCount: document.querySelectorAll("a[href]").length,
                newsLinkCount: Array.from(document.querySelectorAll("a[href]"))
                    .filter(a => a.href.includes("/news/")).length,
                bodyText: (document.body ? document.body.innerText : "").slice(0, 600)
            })
            """
        )
        print(f"\n--- DEBUG [{name}] ---")
        print(f"URL：{info['url']}")
        print(f"標題：{info['title']}")
        print(f"<a> 總數：{info['linkCount']}｜含 /news/ 的連結：{info['newsLinkCount']}")
        print("頁面文字開頭：")
        print(info["bodyText"])
        print("--- END DEBUG ---\n")

        await page.screenshot({"path": os.path.join(OUTPUT_DIR, "debug", f"{name}.png")})
        html = await page.content()
        with open(os.path.join(OUTPUT_DIR, "debug", f"{name}.html"), "w", encoding="utf-8") as f:
            f.write(html)
    except Exception as e:
        print(f"dump_debug 失敗：{e}")


# ============================================================
# 3. 載入更多新聞
# ============================================================

# 不限定標籤：只要「自己的文字」剛好是「載入更多」的可見元素就點
LOAD_MORE_JS = r"""
() => {
    const norm = (t) => (t || "").replace(/\s+/g, "");

    const matches = Array.from(document.querySelectorAll("body *")).filter(el => {
        const own = norm(
            Array.from(el.childNodes)
                .filter(n => n.nodeType === 3)
                .map(n => n.nodeValue)
                .join("")
        );
        return own === "載入更多" || norm(el.textContent) === "載入更多";
    });

    const visible = matches.filter(el => el.getClientRects().length > 0);
    const newsCount = document.querySelectorAll('a[href*="/news/"]').length;

    if (!visible.length) {
        // 找不到時，回報頁面上含「載入」或「更多」的元素，方便除錯
        const near = Array.from(document.querySelectorAll("body *"))
            .filter(el => el.children.length === 0 && /載入|更多|more/i.test(el.textContent || ""))
            .slice(0, 10)
            .map(el => `${el.tagName}.${el.className} → ${(el.textContent || "").trim().slice(0, 30)}`);
        return { found: false, newsCount, near };
    }

    const target = visible[visible.length - 1]; // 文件順序最後 = 最內層
    target.scrollIntoView({ block: "center" });
    target.click();

    return {
        found: true,
        newsCount,
        tag: target.tagName,
        cls: String(target.className || "")
    };
}
"""


async def count_news_links(page):
    return await page.evaluate("() => document.querySelectorAll('a[href*=\"/news/\"]').length")


async def load_more(page, max_clicks=7, wait_time=2):
    print("\n========== 開始載入更多 ==========")

    # 先捲到底，讓延遲載入的按鈕出現
    await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
    await asyncio.sleep(1.5)

    for i in range(max_clicks):
        try:
            info = await page.evaluate(LOAD_MORE_JS)

            if not info.get("found"):
                print("沒有找到「載入更多」，停止。")
                for line in info.get("near", []):
                    print("  疑似元素：", line)
                break

            print(
                f"已點擊載入更多：第 {i + 1} 次"
                f"（{info['tag']}.{info['cls']}，點擊前新聞連結 {info['newsCount']} 個）"
            )
            await asyncio.sleep(wait_time)
            await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")

            after = await count_news_links(page)
            print(f"  點擊後新聞連結：{after} 個")
            if after <= info["newsCount"]:
                print("  ⚠️ 連結數量沒有增加，再等待一下")
                await asyncio.sleep(3)
        except Exception as e:
            print("點擊載入更多失敗：", e)
            break

    print("========== 載入更多結束 ==========\n")


# ============================================================
# 4. 文字 / URL 清理
# ============================================================

def clean_article_title(title):
    if not title:
        return ""
    return re.sub(r"\s+", " ", title).strip()


def clean_ctee_url(url):
    if not url:
        return None

    url = urljoin(BASE_URL, url.strip())

    if "ctee.com.tw" not in url:
        return None

    url = url.split("#")[0]
    lower_url = url.lower()

    bad_patterns = [
        "/livenews", "/search", "/member", "/login", "/register",
        "/subscribe", "/about", "/contact", "/privacy", "/terms",
        "javascript:", "mailto:",
    ]
    if any(p in lower_url for p in bad_patterns):
        return None

    return url


# ============================================================
# 5. 建立 DOM Range，只抓兩個 Marker 之間的 <a>
# ============================================================

CREATE_RANGE_JS = """
(args) => {
    const { startMarker, endMarker } = args;
    const root = document.body;
    if (!root) return { success: false, reason: "沒有 document.body" };

    const walker = document.createTreeWalker(
        root,
        NodeFilter.SHOW_TEXT,
        {
            acceptNode: (node) =>
                node.nodeValue && node.nodeValue.trim()
                    ? NodeFilter.FILTER_ACCEPT
                    : NodeFilter.FILTER_REJECT
        }
    );

    let fullText = "";
    const nodePositions = [];
    let node;
    while (node = walker.nextNode()) {
        const start = fullText.length;
        fullText += node.nodeValue;
        nodePositions.push({ node, start, end: fullText.length });
    }

    const startIndex = fullText.indexOf(startMarker);
    if (startIndex === -1)
        return { success: false, reason: "找不到開始文字", marker: startMarker };

    const endIndex = fullText.indexOf(endMarker, startIndex + startMarker.length);
    if (endIndex === -1)
        return { success: false, reason: "找不到結束文字", marker: endMarker };

    function findPosition(globalIndex) {
        for (const item of nodePositions) {
            if (globalIndex >= item.start && globalIndex <= item.end)
                return { node: item.node, offset: globalIndex - item.start };
        }
        return null;
    }

    const s = findPosition(startIndex);
    const e = findPosition(endIndex);
    if (!s || !e) return { success: false, reason: "無法建立 DOM Range" };

    const range = document.createRange();
    range.setStart(s.node, s.offset);
    range.setEnd(e.node, e.offset);

    const links = [];
    for (const link of root.querySelectorAll("a[href]")) {
        try {
            if (!range.intersectsNode(link)) continue;
            if (!link.href) continue;
            links.push({
                text: (link.innerText || link.textContent || "").trim(),
                href: link.href
            });
        } catch (err) { continue; }
    }

    return { success: true, text: range.toString(), links: links };
}
"""


async def create_text_range(page, start_marker, end_marker):
    return await page.evaluate(
        CREATE_RANGE_JS,
        {"startMarker": start_marker, "endMarker": end_marker},
    )


# ============================================================
# 6. 找最新「盤前」Hub
# ============================================================

FIND_LINKS_JS = """
() => Array.from(document.querySelectorAll("a[href]"))
    .map(a => ({
        title: (a.getAttribute("title") || "").trim(),
        text: (a.innerText || "").trim(),
        textContent: (a.textContent || "").trim(),
        url: a.href
    }))
    .filter(x => x.url)
"""


async def find_latest_premarket_hub(page):
    print("=" * 80)
    print("搜尋最新盤前")
    print("=" * 80)

    await safe_goto(page, LIVE_NEWS_URL)
    await dump_debug(page, "1_livenews_loaded")

    await load_more(page, max_clicks=7, wait_time=2)
    await dump_debug(page, "2_livenews_after_load_more")

    links = await page.evaluate(FIND_LINKS_JS)

    hubs = []
    seen = set()

    for item in links:
        combined = " ".join(
            x for x in (item["title"], item["text"], item["textContent"]) if x
        )
        if "盤前｜" not in re.sub(r"\s+", "", combined):
            continue

        url = item["url"]
        if not url.startswith("https://www.ctee.com.tw/news/"):
            continue
        if url in seen:
            continue
        seen.add(url)

        hubs.append({
            "session": "盤前",
            "title": item["title"] or item["text"] or item["textContent"],
            "url": url,
        })

    if not hubs:
        print("❌ 找不到盤前 Hub")
        sample = [x["title"] or x["text"] for x in links if "/news/" in x["url"]][:15]
        print("頁面上前 15 個新聞連結標題（供對照）：")
        for t in sample:
            print("  -", t)
        return None

    print("\n找到的盤前 Hub")
    print("-" * 80)
    for i, hub in enumerate(hubs, start=1):
        print(f"{i}. {hub['title']}\n   {hub['url']}")
    print("-" * 80)

    # ---- 用標題裡的日期（例如「9／21盤前｜」）挑出今天的盤前 ----
    today = datetime.now(TW_TZ)
    print(f"今天（台灣時間）：{today.month}/{today.day}")

    for hub in hubs:
        m = re.search(r"(\d{1,2})\s*[／/]\s*(\d{1,2})\s*盤前", hub["title"])
        hub["date"] = f"{m.group(1)}/{m.group(2)}" if m else None

    todays = [h for h in hubs if h["date"] == f"{today.month}/{today.day}"]

    if todays:
        chosen = todays[0]
        print(f"\n✅ 找到今天的盤前：{chosen['title']}\n{chosen['url']}")
        return chosen

    latest = hubs[0]
    if ALLOW_STALE:
        print(f"\n⚠️ 今天的盤前尚未發布，測試模式改用最近一篇：{latest['title']}")
        return latest

    print(f"\n❌ 今天的盤前尚未發布（頁面上最新的是：{latest['title']}）")
    return None


# ============================================================
# 7. 從 Hub 抓出內部文章
# ============================================================

async def get_premarket_articles(page, hub):
    print("\n" + "=" * 80)
    print("開始處理最新盤前")
    print("=" * 80)

    await safe_goto(page, hub["url"])
    await dump_debug(page, "3_hub_page")

    result = await create_text_range(page, START_MARKER, END_MARKER)

    if not result.get("success"):
        print("❌ 盤前區間抓取失敗：", result)
        return []

    print(f"指定內容長度：{len(result['text'])} 字")

    raw_links = result.get("links", [])
    print(f"DOM Range 原始連結：{len(raw_links)}")

    articles = []
    seen = set()

    for link in raw_links:
        url = clean_ctee_url(link.get("href"))
        if not url or url == hub["url"]:
            continue
        if not url.startswith("https://www.ctee.com.tw/news/"):
            continue
        if url in seen:
            continue
        seen.add(url)

        title = clean_article_title(link.get("text", ""))
        if not title:
            continue

        articles.append({
            "session": "盤前",
            "hub_title": hub["title"],
            "hub_url": hub["url"],
            "article_title": title,
            "url": url,
        })

    print(f"盤前文章總數：{len(articles)}")
    for i, a in enumerate(articles, start=1):
        print(f"{i}. {a['article_title']}\n   {a['url']}")

    return articles


# ============================================================
# 8. 主程式
# ============================================================

async def run_once():
    browser = None
    try:
        browser = await create_browser()
        page = await browser.newPage()
        await prepare_page(page, browser)

        deadline = time.monotonic() + MAX_WAIT_MINUTES * 60
        while True:
            hub = await find_latest_premarket_hub(page)
            if hub:
                break
            if time.monotonic() >= deadline:
                return {"hub": None, "articles": []}
            print("5 分鐘後重新檢查…")
            await asyncio.sleep(300)

        articles = await get_premarket_articles(page, hub)

        return {
            "scraped_at": datetime.now(TW_TZ).isoformat(),
            "hub": hub,
            "articles": articles,
        }
    finally:
        if browser:
            await browser.close()
            print("\nChrome 已關閉")


async def main(max_attempts=3):
    """被 Cloudflare 擋下時，換一個全新的瀏覽器工作階段重試。"""
    for attempt in range(1, max_attempts + 1):
        try:
            return await run_once()
        except BlockedError as e:
            print(f"❌ 第 {attempt}/{max_attempts} 次嘗試失敗：{e}")
            if attempt < max_attempts:
                print("30 秒後以全新瀏覽器重試…")
                await asyncio.sleep(30)
    return {"hub": None, "articles": [], "blocked": True}


def save_result(result):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n已儲存：{OUTPUT_FILE}")


if __name__ == "__main__":
    result = asyncio.run(main())
    save_result(result)

    # 沒抓到文章就回傳失敗碼，讓 GitHub Actions 顯示紅燈，也不會繼續往下寄空信
    if not result.get("articles"):
        print("❌ 沒有抓到任何文章")
        sys.exit(1)
