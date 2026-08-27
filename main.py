#!/usr/bin/env python3
"""邮箱论文整理 Agent — 读取谷歌学术邮件 → LLM 筛选 + 翻译 → 生成静态 HTML"""

import sys
import json
import imaplib
import email
import ssl
import re
import os
import html as html_mod
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yaml
from bs4 import BeautifulSoup
from openai import OpenAI

# ── Config ──────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
STATE_FILE = SCRIPT_DIR / "state.json"


def load_config():
    for name in ["config.local.yaml", "config.yaml"]:
        p = SCRIPT_DIR / name
        if p.exists():
            raw = p.read_text(encoding="utf-8")
            raw = re.sub(r"\$\{\{?\s*(\w+)\s*\}?\}", lambda m: os.environ.get(m.group(1), ""), raw)
            return yaml.safe_load(raw)
    sys.exit("找不到 config.yaml 或 config.local.yaml")


cfg = load_config()
EMAIL_CFG = cfg["email"]
LLM_CFG = cfg["llm"]
PROFILE = cfg["profile"]
OUTPUT_DIR = (SCRIPT_DIR / cfg["output"]["dir"]).resolve()
MAX_PAPERS = cfg["output"]["max_papers_per_run"]
MIN_SCORE = PROFILE["min_score"]

TZ = timezone(timedelta(hours=8))
TODAY = datetime.now(TZ).strftime("%Y-%m-%d")

EMAIL_CFG["password"] = os.environ.get("QQ_PASSWORD", EMAIL_CFG.get("password", ""))
LLM_CFG["api_key"] = os.environ.get("LLM_API_KEY", LLM_CFG.get("api_key", ""))


# ── State ───────────────────────────────────────────────────────────────


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"processed_uids": [], "last_run": ""}


def save_state(state):
    state["last_run"] = TODAY
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ── IMAP ────────────────────────────────────────────────────────────────


def connect_imap():
    ctx = ssl.create_default_context()
    conn = imaplib.IMAP4_SSL(EMAIL_CFG["imap_server"], EMAIL_CFG["imap_port"], ssl_context=ctx)
    conn.login(EMAIL_CFG["username"], EMAIL_CFG["password"])
    return conn


def fetch_scholar_emails(conn, processed_uids):
    """获取未处理的谷歌学术邮件，返回 [(uid, email_message), ...]，最新优先"""
    conn.select("INBOX")
    status, data = conn.uid("search", None, f'FROM "{EMAIL_CFG["sender_filter"]}"')
    if status != "OK":
        return []

    all_uids = data[0].split()
    if not all_uids:
        return []

    processed_set = set(processed_uids)
    unprocessed = [uid for uid in all_uids if int(uid) not in processed_set]
    latest = unprocessed[-100:] if len(unprocessed) > 100 else unprocessed
    latest.reverse()

    results = []
    for uid in latest:
        status, msg_data = conn.uid("fetch", uid, "(RFC822)")
        if status != "OK":
            continue
        raw_email = msg_data[0][1]
        msg = email.message_from_bytes(raw_email)
        results.append((int(uid), msg))
    return results


# ── Email Parsing ────────────────────────────────────────────────────────


def get_email_body(msg):
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode(errors="replace")
    return ""


def extract_papers(html_body):
    """从谷歌学术邮件 HTML 中提取论文列表 [{title, url, snippet}, ...]"""
    soup = BeautifulSoup(html_body, "html.parser")
    papers = []
    seen = set()

    for h3 in soup.find_all("h3"):
        a = h3.find("a", href=True)
        if not a:
            continue
        title = a.get_text(strip=True)
        url = a["href"]
        if not title or url in seen:
            continue
        seen.add(url)

        snippet = ""
        node = h3.next_sibling
        parts = []
        while node and len(parts) < 200:
            if node.name == "h3":
                break
            text = node.get_text(strip=True) if hasattr(node, "get_text") else str(node).strip()
            if text:
                parts.append(text)
            node = node.next_sibling
        snippet = " ".join(parts)[:500]

        papers.append({"title": title, "url": url, "snippet": snippet})

    if not papers:
        for a in soup.find_all("a", href=True):
            href = a["href"]
            title = a.get_text(strip=True)
            if not title or len(title) < 10:
                continue
            if any(skip in href.lower() for skip in ["google.com/scholar", "scholar.google.", "unsubscribe"]):
                continue
            if href in seen:
                continue
            seen.add(href)
            papers.append({"title": title, "url": href, "snippet": ""})

    return papers


# ── LLM ──────────────────────────────────────────────────────────────────


def build_llm_client():
    return OpenAI(base_url=LLM_CFG["api_base"], api_key=LLM_CFG["api_key"])


SCORE_PROMPT = """你是一个学术论文筛选助手。用户的研究兴趣如下：

{interest}

请对以下论文进行相关性评分（1-10 分）并翻译标题。仅当评分 >= {min_score} 时才翻译摘要。

论文标题: {title}
摘要/片段: {snippet}

请严格返回 JSON 对象（不要包含 markdown 代码块标记）：
{{"score": <1-10 整数>, "reason": "<一句话理由>", "title_cn": "<中文标题翻译>"}}
如果评分 >= {min_score}，额外加上 "abstract_cn": "<中文摘要翻译>"。"""


def score_paper(client, paper):
    prompt = SCORE_PROMPT.format(
        interest=PROFILE["interest"],
        min_score=MIN_SCORE,
        title=paper["title"],
        snippet=paper["snippet"] or paper["title"],
    )
    try:
        resp = client.chat.completions.create(
            model=LLM_CFG["model"],
            temperature=LLM_CFG.get("temperature", 0.3),
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            timeout=60,
        )
        content = resp.choices[0].message.content.strip()
        if content.startswith("```"):
            content = re.sub(r"^```\w*\n?", "", content)
            content = re.sub(r"\n```$", "", content)
        return json.loads(content)
    except Exception as e:
        print(f"  [ERROR] LLM 调用失败: {e}")
        return {"score": 0, "reason": f"LLM error: {e}", "title_cn": paper["title"]}


def process_papers(papers):
    """处理论文列表，返回 {interesting: [...], boring: [...]}"""
    client = build_llm_client()
    interesting, boring = [], []

    for i, p in enumerate(papers):
        result = score_paper(client, p)
        result["title"] = p["title"]
        result["url"] = p["url"]
        result["snippet"] = p["snippet"]

        if result.get("score", 0) >= MIN_SCORE:
            interesting.append(result)
        else:
            boring.append(result)

    interesting.sort(key=lambda x: x.get("score", 0), reverse=True)
    return {"interesting": interesting, "boring": boring}


# ── HTML Generation ──────────────────────────────────────────────────────

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>论文摘要 — {date}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; background: #fafafa; color: #333; }}
  h1 {{ font-size: 1.5em; border-bottom: 2px solid #2563eb; padding-bottom: 8px; }}
  h2 {{ font-size: 1.1em; margin-top: 30px; color: #666; }}
  .paper {{ margin: 16px 0; padding: 12px 16px; background: #fff; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  .paper a {{ color: #2563eb; text-decoration: none; font-weight: 600; }}
  .paper a:hover {{ text-decoration: underline; }}
  .meta {{ font-size: .8em; color: #999; margin-top: 4px; }}
  .reason {{ font-size: .85em; color: #666; margin-top: 4px; }}
  .abstract {{ font-size: .9em; color: #555; margin-top: 8px; line-height: 1.6; }}
  .boring {{ opacity: .7; }}
  .score {{ display: inline-block; background: #dbeafe; color: #1e40af; padding: 1px 6px; border-radius: 4px; font-size: .75em; margin-left: 4px; }}
  .count {{ font-size: .9em; color: #888; }}
  .footer {{ margin-top: 40px; padding-top: 16px; border-top: 1px solid #e5e5e5; font-size: .8em; color: #aaa; }}
  .footer a {{ color: #aaa; }}
</style>
</head>
<body>
<h1>📄 论文摘要 — {date}</h1>
<p class="count">本次共 {total} 篇 | 感兴趣 {n_interesting} 篇 | 其他 {n_boring} 篇</p>

<h2>⭐ 可能感兴趣的论文 ({n_interesting})</h2>
{interesting_html}

<h2>📋 其他论文 ({n_boring})</h2>
{boring_html}

<div class="footer">
  <p>由 AI 自动生成于 {date}</p>
</div>
</body>
</html>"""

PAPER_HTML = """<div class="paper {cls}">
  <div><a href="{url}" target="_blank">{title}</a><span class="score">{score}/10</span></div>
  <div class="meta">🇨🇳 {title_cn}</div>
  <div class="reason">📝 {reason}</div>
  {abstract_html}
</div>"""


def render_papers(papers, cls=""):
    parts = []
    for p in papers:
        abstract_html = ""
        if p.get("abstract_cn"):
            abstract_html = f'<div class="abstract">{html_mod.escape(p["abstract_cn"])}</div>'
        parts.append(
            PAPER_HTML.format(
                cls=cls,
                url=html_mod.escape(p.get("url", "#")),
                title=html_mod.escape(p.get("title", "")),
                score=p.get("score", "?"),
                title_cn=html_mod.escape(p.get("title_cn", "")),
                reason=html_mod.escape(p.get("reason", "")),
                abstract_html=abstract_html,
            )
        )
    return "\n".join(parts) if parts else '<p style="color:#999">暂无</p>'


def generate_html(results):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    yesterday = (datetime.now(TZ) - timedelta(days=1)).strftime("%Y-%m-%d")

    interesting_html = render_papers(results["interesting"])
    boring_html = render_papers(results["boring"], cls="boring")

    html = HTML_TEMPLATE.format(
        date=TODAY,
        total=len(results["interesting"]) + len(results["boring"]),
        n_interesting=len(results["interesting"]),
        n_boring=len(results["boring"]),
        interesting_html=interesting_html,
        boring_html=boring_html,
        yesterday=yesterday,
    )

    (OUTPUT_DIR / "index.html").write_text(html, encoding="utf-8")
    (OUTPUT_DIR / f"digest-{TODAY}.html").write_text(html, encoding="utf-8")
    print(f"生成 HTML: output/index.html, output/digest-{TODAY}.html")


# ── Pipeline ─────────────────────────────────────────────────────────────


def dedup_papers(papers):
    seen = set()
    unique = []
    for p in papers:
        if p["url"] not in seen:
            seen.add(p["url"])
            unique.append(p)
    return unique


def run_pipeline(progress_callback=None):
    """核心 pipeline：连接邮箱 → 逐个取未处理邮件累积论文 → 满 MAX_PAPERS 后处理一批即停
    第二天运行时从未处理的下一封继续。progress_callback(stage, current, total, message)"""
    def progress(stage, current=0, total=0, message=""):
        print(f"  [{stage}] {message}")
        if progress_callback:
            progress_callback(stage, current, total, message)

    state = load_state()
    progress("connecting", message=f"已处理 {len(state['processed_uids'])} 封邮件")

    conn = connect_imap()
    try:
        emails = fetch_scholar_emails(conn, state["processed_uids"])
        progress("fetching", message=f"找到 {len(emails)} 封未处理邮件")

        if not emails:
            generate_html({"interesting": [], "boring": []})
            progress("done", message="无新邮件")
            return {"interesting": [], "boring": []}

        pending_papers = []
        processed_uids_batch = []
        total_processed = 0

        for uid, msg in emails:
            body = get_email_body(msg)
            papers = extract_papers(body)
            print(f"  邮件 UID={uid} [{msg['Subject']}] → {len(papers)} 篇论文")
            pending_papers.extend(papers)
            processed_uids_batch.append(uid)

            # 满阈值：处理这批后停止，明天继续
            if len(pending_papers) >= MAX_PAPERS:
                break

        # 处理这一批（去重后可能略多于 MAX_PAPERS，因为完整收了最后一封邮件）
        unique = dedup_papers(pending_papers)
        total_processed = len(unique)
        progress("processing", total_processed, total_processed, f"处理 {len(unique)} 篇论文...")

        results = process_papers(unique)
        results["interesting"].sort(key=lambda x: x.get("score", 0), reverse=True)

        # 生成 HTML
        progress("generating", message="生成 HTML...")
        generate_html(results)

        # HTML 生成后保存状态
        state["processed_uids"].extend(processed_uids_batch)
        save_state(state)

        total = len(results["interesting"]) + len(results["boring"])
        progress("done", message=f"完成: {len(results['interesting'])} 感兴趣 / {total} 总计")
        return results

    finally:
        conn.logout()


# ── CLI ──────────────────────────────────────────────────────────────────


def mock_results(papers):
    results = {"interesting": [], "boring": []}
    for i, p in enumerate(papers):
        if i < 2:
            results["interesting"].append({
                "title": p["title"], "url": p["url"], "snippet": p["snippet"],
                "score": 8 - i,
                "reason": "与无线光通信方向高度相关" if i == 0 else "涉及光束指向技术",
                "title_cn": f"[MOCK] {p['title']}",
                "abstract_cn": f"[MOCK 中文摘要] {p['snippet']}",
            })
        else:
            results["boring"].append({
                "title": p["title"], "url": p["url"], "snippet": p["snippet"],
                "score": 3,
                "reason": "与用户研究方向无关",
                "title_cn": f"[MOCK] {p['title']}",
            })
    return results


MOCK_PAPERS = [
    {"title": "Free-Space Optical Communication with Adaptive Beam Pointing for Satellite Links",
     "url": "https://arxiv.org/abs/example1",
     "snippet": "We demonstrate a free-space optical communication system with adaptive beam pointing..."},
    {"title": "Acquisition and Tracking Algorithms for Deep-Space Laser Communications",
     "url": "https://arxiv.org/abs/example2",
     "snippet": "This paper presents novel acquisition and tracking algorithms for deep-space lasercom..."},
    {"title": "Atmospheric Turbulence Compensation Using Adaptive Optics for FSO Links",
     "url": "https://arxiv.org/abs/example3",
     "snippet": "We analyze adaptive optics methods for compensating atmospheric turbulence in free-space optical links..."},
]


def main():
    dry_run = "--dry-run" in sys.argv
    print(f"{'[DRY RUN] ' if dry_run else ''}论文整理 Agent — {TODAY}")
    print(f"兴趣阈值: >= {MIN_SCORE}/10")

    if dry_run:
        if not LLM_CFG.get("api_key"):
            print("未设置 LLM_API_KEY，使用模拟结果生成 HTML")
            results = mock_results(MOCK_PAPERS)
        else:
            results = process_papers(MOCK_PAPERS)
        generate_html(results)
        return

    run_pipeline()


if __name__ == "__main__":
    main()
