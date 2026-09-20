"""
診斷用：看看 GitHub 的機器能不能「不經瀏覽器」直接讀到工商時報的各種頁面。
只用標準函式庫，不需要安裝任何東西。
"""
import re
import urllib.request
import urllib.error

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)

URLS = {
    "RSS（證券）": "https://www.ctee.com.tw/rss_web/livenews/stock",
    "即時新聞頁": "https://www.ctee.com.tw/livenews/stock",
    "盤前 Hub 文章": "https://www.ctee.com.tw/news/20260907700331-430201",
    "首頁": "https://www.ctee.com.tw/",
}

CHALLENGE_MARKS = ("正在執行安全驗證", "Just a moment", "cf-chl", "challenge-platform")


def fetch(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "zh-TW,zh;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return None, str(e)


def main():
    status, ip = fetch("https://api.ipify.org")
    print(f"本次執行的對外 IP：{ip.strip() if status == 200 else '取得失敗'}\n")

    for name, url in URLS.items():
        status, body = fetch(url)
        blocked = any(m in body for m in CHALLENGE_MARKS)
        title = re.search(r"<title>(.*?)</title>", body, re.S)
        print(f"[{name}] {url}")
        print(f"  HTTP 狀態：{status}｜長度：{len(body):,}｜被 Cloudflare 擋：{'是' if blocked else '否'}")
        if title:
            print(f"  標題：{title.group(1).strip()[:60]}")
        if name.startswith("RSS") and not blocked:
            print(f"  RSS <item> 數量：{body.count('<item>')}")
        print()


if __name__ == "__main__":
    main()
