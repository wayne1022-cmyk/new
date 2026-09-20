"""
第三步：讀取 analyze.py 產生的摘要 → 組成 HTML → 用 Gmail 寄出
輸入：output/premarket_summaries.json
環境變數（GitHub Secrets）：MAIL_FROM、MAIL_APP_PASSWORD、MAIL_TO
"""

import json
import os
import smtplib
import sys
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

# ============================================================
# 1. 基本設定
# ============================================================

RESULT_FILE = os.path.join("output", "premarket_summaries.json")

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 465


def get_mail_config():
    mail_from = os.environ.get("MAIL_FROM", "").strip()
    mail_password = os.environ.get("MAIL_APP_PASSWORD", "").strip()
    mail_to_raw = os.environ.get("MAIL_TO", "").strip()

    if not mail_from:
        raise ValueError("找不到 MAIL_FROM，請在 GitHub Secrets 設定寄件者 Email。")
    if not mail_password:
        raise ValueError("找不到 MAIL_APP_PASSWORD，請在 GitHub Secrets 設定 Gmail 應用程式密碼。")
    if not mail_to_raw:
        raise ValueError("找不到 MAIL_TO，請在 GitHub Secrets 設定收件者 Email。")

    # 支援多個收件者，用逗號分隔
    mail_to = [x.strip() for x in mail_to_raw.split(",") if x.strip()]

    return mail_from, mail_password, mail_to


# ============================================================
# 2. 組 HTML
# ============================================================

CSS = """
body { font-family: Arial, "Microsoft JhengHei", sans-serif; line-height: 1.6; color: #333333; }
.container { max-width: 900px; margin: auto; }
.header { padding: 20px; border-bottom: 2px solid #1F4E79; }
.header h1 { color: #1F4E79; margin-bottom: 10px; }
.article { margin-top: 25px; padding: 20px; border: 1px solid #dddddd; border-radius: 8px; }
.article-title { font-size: 20px; font-weight: bold; color: #1F4E79; }
.meta { color: #666666; font-size: 14px; margin-top: 5px; margin-bottom: 15px; }
.summary { background-color: #f7f7f7; padding: 15px; border-radius: 6px; }
.article-link { margin-top: 15px; }
.article-link a { color: #1F4E79; }
.footer { margin-top: 30px; padding-top: 15px; border-top: 1px solid #dddddd; color: #777777; font-size: 13px; }
"""


def build_html(hub, articles, success_count, failed_count):
    parts = [f"""
<html>
<head>
<meta charset="UTF-8">
<style>{CSS}</style>
</head>
<body>
<div class="container">

<div class="header">
<h1>工商時報｜盤前新聞 AI 摘要</h1>
<p><strong>Hub：</strong>{escape(hub.get("title", ""))}</p>
<p>
<strong>文章數：</strong>{len(articles)} 篇　
<strong>AI 成功：</strong>{success_count} 篇　
<strong>AI 失敗：</strong>{failed_count} 篇
</p>
</div>
"""]

    for i, article in enumerate(articles, start=1):
        title = escape(article.get("article_title", ""))
        author = escape(article.get("author", ""))
        publish_time = escape(article.get("publish_time", ""))
        url = escape(article.get("url", ""), quote=True)

        if article.get("ai_status") == "success":
            summary_html = escape(article.get("summary", "")).replace("\n", "<br>")
        else:
            error = escape(article.get("ai_error") or article.get("scrape_error") or "未知錯誤")
            summary_html = f"<strong>⚠️ AI 摘要失敗</strong><br>{error}"

        parts.append(f"""
<div class="article">
    <div class="article-title">{i}. {title}</div>
    <div class="meta">作者：{author}<br>時間：{publish_time}</div>
    <div class="summary">{summary_html}</div>
    <div class="article-link"><a href="{url}">閱讀工商時報原文</a></div>
</div>
""")

    parts.append("""
<div class="footer">
本郵件由 CTEE News AI 自動整理系統產生。<br>
新聞內容來源：工商時報<br>
AI 摘要僅根據原始文章內容整理。
</div>

</div>
</body>
</html>
""")

    return "".join(parts)


def build_plain_text(hub, articles):
    """純文字版本，給不支援 HTML 的信箱當備援。"""
    lines = [f"工商時報｜盤前新聞 AI 摘要", hub.get("title", ""), ""]
    for i, a in enumerate(articles, start=1):
        lines.append(f"{i}. {a.get('article_title', '')}")
        if a.get("ai_status") == "success":
            lines.append(a.get("summary", ""))
        else:
            lines.append("（AI 摘要失敗）")
        lines.append(a.get("url", ""))
        lines.append("")
    return "\n".join(lines)


# ============================================================
# 3. 寄信
# ============================================================

def send_email(mail_from, mail_password, mail_to, subject, html, plain):
    message = MIMEMultipart("alternative")
    message["From"] = mail_from
    message["To"] = ", ".join(mail_to)
    message["Subject"] = Header(subject, "utf-8")

    # 順序：純文字在前、HTML 在後（客戶端會優先顯示最後一個）
    message.attach(MIMEText(plain, "plain", "utf-8"))
    message.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=60) as server:
        server.login(mail_from, mail_password)
        server.sendmail(mail_from, mail_to, message.as_string())


# ============================================================
# 4. 主程式
# ============================================================

def main():
    mail_from, mail_password, mail_to = get_mail_config()

    if not os.path.exists(RESULT_FILE):
        print(f"❌ 找不到 {RESULT_FILE}，請先執行 analyze.py")
        sys.exit(1)

    with open(RESULT_FILE, "r", encoding="utf-8") as f:
        result_ai = json.load(f)

    hub = result_ai["hub"]
    articles = result_ai["articles"]

    success_count = sum(1 for a in articles if a.get("ai_status") == "success")
    failed_count = len(articles) - success_count

    html = build_html(hub, articles, success_count, failed_count)
    plain = build_plain_text(hub, articles)
    subject = f"【工商時報｜盤前 AI 摘要】{hub.get('title', '')}"

    print("=" * 80)
    print("開始寄送 Email")
    print("=" * 80)
    print(f"收件者數量：{len(mail_to)}")
    print(f"文章數：{len(articles)}｜AI 成功：{success_count}｜AI 失敗：{failed_count}")

    try:
        send_email(mail_from, mail_password, mail_to, subject, html, plain)
        print("\n✅ Email 寄送成功！")
    except Exception as e:
        print(f"\n❌ Email 寄送失敗：{e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
