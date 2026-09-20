"""
第二步：讀取 ctee_scraper.py 產生的文章清單 → 逐篇抓內文 → 送 Groq 摘要
輸入：output/premarket_articles.json
輸出：output/premarket_summaries.json
"""

import asyncio
import json
import os
import re
import sys

from groq import Groq

# 沿用第一個檔案的 Chrome 尋找邏輯與 User-Agent
from ctee_scraper import (
    BlockedError,
    close_browser,
    create_browser,
    prepare_page,
    safe_goto,
)

# ============================================================
# 1. 基本設定
# ============================================================

INPUT_FILE = os.path.join("output", "premarket_articles.json")
OUTPUT_FILE = os.path.join("output", "premarket_summaries.json")

# 模型名稱可用環境變數 GROQ_MODEL 覆蓋，方便之後換模型不用改程式
GROQ_MODEL = os.environ.get("GROQ_MODEL", "qwen/qwen3.8-27b")

# 單篇文章送給 AI 的字數上限，避免超過 token 限制
MAX_CONTENT_CHARS = 12000

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
if not GROQ_API_KEY:
    raise ValueError(
        "找不到 GROQ_API_KEY。\n"
        "請到 GitHub repo → Settings → Secrets and variables → Actions 新增。"
    )

groq_client = Groq(api_key=GROQ_API_KEY)


# ============================================================
# 2. 瀏覽器（沿用 ctee_scraper 的設定：有畫面 Chrome + Cloudflare 等待）
# ============================================================


def clean_text(text):
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


# ============================================================
# 3. 抓取單篇文章
# ============================================================

# 注意：這裡用 raw string（r"""），否則 \s 會被 Python 吃掉，
# 導致 JS 的空白清理失效。
EXTRACT_JS = r"""
() => {
    function cleanText(text) {
        if (!text) return "";
        return text.replace(/\s+/g, " ").trim();
    }

    // ---- 標題 ----
    let title = "";
    for (const selector of [
        "h1", "article h1",
        "[class*='article-title']", "[class*='articleTitle']"
    ]) {
        const el = document.querySelector(selector);
        if (el) {
            const t = cleanText(el.innerText);
            if (t.length > 0) { title = t; break; }
        }
    }

    // ---- 內文 ----
    let content = "";
    for (const selector of [
        "article",
        "[class*='article-content']", "[class*='articleContent']",
        "[class*='content']", "[class*='story']", "[class*='post-content']"
    ]) {
        for (const el of document.querySelectorAll(selector)) {
            const t = cleanText(el.innerText);
            if (t.length > content.length && t.length > 200) content = t;
        }
    }
    if (content.length < 200 && document.body) {
        content = cleanText(document.body.innerText);
    }

    // ---- 作者 ----
    let author = "";
    for (const selector of [
        "[class*='author']", "[class*='Author']",
        "[class*='writer']", "[class*='Writer']"
    ]) {
        const el = document.querySelector(selector);
        if (el) {
            const t = cleanText(el.innerText);
            if (t.length > 0 && t.length < 200) { author = t; break; }
        }
    }

    // ---- 時間 ----
    let publish_time = "";
    const timeEl = document.querySelector("time");
    if (timeEl) {
        publish_time = timeEl.getAttribute("datetime") || cleanText(timeEl.innerText);
    }

    return { title, author, publish_time, content };
}
"""


async def scrape_article(page, article):
    url = article["url"]

    print("\n" + "=" * 80)
    print(f"開始抓取：{article['article_title']}")
    print(f"URL：{url}")
    print("=" * 80)

    try:
        await safe_goto(page, url)

        data = await page.evaluate(EXTRACT_JS)

        title = clean_text(data.get("title", ""))
        author = clean_text(data.get("author", ""))
        publish_time = clean_text(data.get("publish_time", ""))
        content = clean_text(data.get("content", ""))

        print(f"標題：{title}")
        print(f"作者：{author}")
        print(f"時間：{publish_time}")
        print(f"文章長度：{len(content):,} 字")

        if len(content) < 200:
            print("⚠️ 文章內容過短，可能抓取失敗。")

        return {
            **article,
            "scraped_title": title,
            "author": author,
            "publish_time": publish_time,
            "content": content,
            "scrape_status": "success",
        }

    except Exception as e:
        print(f"❌ 抓取失敗：{e}")
        return {
            "blocked": isinstance(e, BlockedError),
            **article,
            "scraped_title": "",
            "author": "",
            "publish_time": "",
            "content": "",
            "scrape_status": "failed",
            "scrape_error": str(e),
        }


# ============================================================
# 4. Groq 摘要
# ============================================================

SYSTEM_PROMPT = (
    "你是一名專業的台灣財經新聞摘要助理。"
    "請忠實根據使用者提供的文章進行摘要，不得捏造文章不存在的資訊。"
)

PROMPT_TEMPLATE = """
你是一名專業的台灣財經新聞分析助理。

請根據以下工商時報文章內容，製作一份「精簡但資訊完整」的新聞摘要。

請嚴格遵守以下規則：

1. 只能根據提供的文章內容整理。
2. 不得自行加入文章沒有提到的資訊。
3. 不得自行推測或補充背景資料。
4. 保留重要的人名、公司名稱、政策名稱。
5. 保留重要數字、百分比、金額、日期與時間。
6. 如果文章提到股市、產業或個別公司影響，請整理原文所述內容。
7. 如果原文沒有說明影響，不要自行推論。
8. 使用繁體中文。
9. 摘要要精簡，但不能犧牲重要資訊。
10. 不需要評論新聞好不好，也不要加入你的個人意見。

請使用以下格式：

【新聞重點】
-
-
-

【重要數據／人物／公司】
-
-
-

【市場／產業影響】
-

如果文章沒有明確提到市場或產業影響，請寫：
「原文未明確說明。」

文章標題：
{title}

文章內容：
{content}
"""


def call_groq(prompt, max_retries=3):
    """遇到限流（429）等暫時性錯誤時自動重試。"""
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            response = groq_client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                max_completion_tokens=1200,
            )
            text = response.choices[0].message.content or ""

            # 部分推理模型（如 Qwen3）會輸出 <think>...</think>，摘要中不需要
            text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
            return text.strip()

        except Exception as e:
            last_error = e
            wait = 10 * attempt
            print(f"⚠️ Groq 第 {attempt} 次失敗：{e}；{wait} 秒後重試")
            import time
            time.sleep(wait)

    raise last_error


def summarize_article(article):
    content = article.get("content", "")

    if not content:
        return {**article, "summary": "", "ai_status": "failed", "ai_error": "沒有文章內容"}

    print(f"\n🤖 送給 Groq：{article['article_title']}")

    prompt = PROMPT_TEMPLATE.format(
        title=article["article_title"],
        content=content[:MAX_CONTENT_CHARS],
    )

    try:
        summary = call_groq(prompt)

        if not summary:
            raise ValueError("AI 回傳空內容")

        print("✅ 摘要完成")
        print("-" * 80)
        print(summary)
        print("-" * 80)

        return {**article, "summary": summary, "ai_status": "success"}

    except Exception as e:
        print(f"❌ Groq 失敗：{e}")
        return {**article, "summary": "", "ai_status": "failed", "ai_error": str(e)}


# ============================================================
# 5. 單篇處理 / 全部處理
# ============================================================

async def process_article(page, article):
    result = await scrape_article(page, article)

    if result["scrape_status"] == "success":
        # Groq SDK 是同步的，放到 thread 執行避免卡住 event loop
        result = await asyncio.to_thread(summarize_article, result)
    else:
        result["summary"] = ""
        result["ai_status"] = "skipped"

    return result


async def process_all_articles(article_data):
    browser = await create_browser()
    page = await browser.newPage()
    await prepare_page(page, browser)

    results = []
    blocked_streak = 0
    total = len(article_data["articles"])

    print("\n" + "=" * 80)
    print(f"開始處理盤前文章，共 {total} 篇")
    print("=" * 80)

    try:
        for i, article in enumerate(article_data["articles"], start=1):
            print("\n" + "#" * 80)
            print(f"進度：{i} / {total}")
            print("#" * 80)

            result = await process_article(page, article)
            results.append(result)

            # 連續被 Cloudflare 擋下就不要再浪費時間了
            blocked_streak = blocked_streak + 1 if result.get("blocked") else 0
            if blocked_streak >= 2:
                print("❌ 連續 2 篇被 Cloudflare 擋下，停止抓取剩餘文章")
                for rest in article_data["articles"][i:]:
                    results.append({**rest, "content": "", "summary": "",
                                    "scrape_status": "failed", "ai_status": "skipped",
                                    "scrape_error": "被 Cloudflare 擋下，未嘗試"})
                break

            await asyncio.sleep(1)  # 避免連續快速打 API
    finally:
        await close_browser(browser)

    scrape_success = sum(1 for x in results if x.get("scrape_status") == "success")
    ai_success = sum(1 for x in results if x.get("ai_status") == "success")

    print("\n" + "=" * 80)
    print("全部文章處理完成")
    print(f"文章總數：{total}｜成功抓取：{scrape_success}｜AI 成功：{ai_success}｜AI 失敗：{total - ai_success}")
    print("=" * 80)

    return {
        "scraped_at": article_data.get("scraped_at"),
        "hub": article_data["hub"],
        "articles": results,
        "stats": {
            "total": total,
            "scrape_success": scrape_success,
            "ai_success": ai_success,
        },
    }


# ============================================================
# 6. 主程式
# ============================================================

def main():
    if not os.path.exists(INPUT_FILE):
        print(f"❌ 找不到 {INPUT_FILE}，請先執行 ctee_scraper.py")
        sys.exit(1)

    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        article_data = json.load(f)

    if not article_data.get("articles"):
        print("❌ 文章清單是空的")
        sys.exit(1)

    result_ai = asyncio.run(process_all_articles(article_data))

    os.makedirs("output", exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result_ai, f, ensure_ascii=False, indent=2)
    print(f"\n已儲存：{OUTPUT_FILE}")

    # 一篇摘要都沒成功就視為失敗，避免寄出空信
    if result_ai["stats"]["ai_success"] == 0:
        print("❌ 沒有任何一篇 AI 摘要成功")
        sys.exit(1)


if __name__ == "__main__":
    main()
