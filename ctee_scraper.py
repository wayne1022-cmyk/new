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
        headless=True,
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


async def safe_goto(page, url):
    """networkidle2 在廣告多的網站可能逾時，失敗就退而求其次。"""
    try:
        await page.goto(url, {"waitUntil": "networkidle2", "timeout": 60000})
    except Exception as e:
        print(f"networkidle2 逾時（{e}），改用 domcontentloaded 重試")
        await page.goto(url, {"waitUntil": "domcontentloaded", "timeout": 60000})
    await asyncio.sleep(3)


# ============================================================
# 3. 載入更多新聞
# ============================================================

async def load_more(page, max_clicks=7, wait_time=2):
    print("\n========== 開始載入更多 ==========")

    js = """
    () => {
        const els = Array.from(
            document.querySelectorAll("button, a, [role='button']")
        );
        const target = els.find(
            e => (e.innerText || e.textContent || '').includes('載入更多')
        );
        if (!target) return false;
        target.scrollIntoView({block: 'center'});
        target.click();
        return true;
    }
    """

    for i in range(max_clicks):
        try:
            clicked = await page.evaluate(js)
            if not clicked:
                print("沒有找到「載入更多」，停止。")
                break
            print(f"已點擊載入更多：第 {i + 1} 次")
            await asyncio.sleep(wait_time)
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
    await load_more(page, max_clicks=7, wait_time=2)

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
        return None

    print("\n找到的盤前 Hub")
    print("-" * 80)
    for i, hub in enumerate(hubs, start=1):
        print(f"{i}. {hub['title']}\n   {hub['url']}")
    print("-" * 80)

    latest = hubs[0]  # 頁面由新到舊，第一個視為最新
    print(f"\n最新盤前：{latest['title']}\n{latest['url']}")
    return latest


# ============================================================
# 7. 從 Hub 抓出內部文章
# ============================================================

async def get_premarket_articles(page, hub):
    print("\n" + "=" * 80)
    print("開始處理最新盤前")
    print("=" * 80)

    await safe_goto(page, hub["url"])

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

async def main():
    browser = None
    try:
        browser = await create_browser()
        page = await browser.newPage()
        await page.setUserAgent(USER_AGENT)
        await page.setViewport({"width": 1920, "height": 1080})

        hub = await find_latest_premarket_hub(page)
        if not hub:
            return {"hub": None, "articles": []}

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
