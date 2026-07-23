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


def load_config():
    """加载配置，本地文件优先"""
    for name in ["config.local.yaml", "config.yaml"]:
        p = SCRIPT_DIR / name
        if p.exists():
            raw = p.read_text(encoding="utf-8")
            # 替换环境变量占位符 ${VAR} 或 $VAR
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

TZ = timezone(timedelta(hours=8))  # 北京时间
TODAY = datetime.now(TZ).strftime("%Y-%m-%d")

# 环境变量覆盖敏感信息
EMAIL_CFG["password"] = os.environ.get("QQ_PASSWORD", EMAIL_CFG.get("password", ""))
LLM_CFG["api_key"] = os.environ.get("LLM_API_KEY", LLM_CFG.get("api_key", ""))

# ── IMAP ────────────────────────────────────────────────────────────────


def connect_imap():
    """连接并登录 QQ 邮箱 IMAP"""
    ctx = ssl.create_default_context()
    conn = imaplib.IMAP4_SSL(EMAIL_CFG["imap_server"], EMAIL_CFG["imap_port"], ssl_context=ctx)
    conn.login(EMAIL_CFG["username"], EMAIL_CFG["password"])
    return conn


def fetch_scholar_emails(conn):
    """获取未读的谷歌学术邮件，返回 [(msg_id, email_message), ...]"""
    conn.select("INBOX")
    # 搜索来自 google scholar 的未读邮件
    status, data = conn.search(None, "UNSEEN", f'FROM "{EMAIL_CFG["sender_filter"]}"')
    if status != "OK":
        return []

    msg_ids = data[0].split()
    if not msg_ids:
        return []

    results = []
    for mid in msg_ids[-MAX_PAPERS:]:  # 只取最近若干封
        status, msg_data = conn.fetch(mid, "(RFC822)")
        if status != "OK":
            continue
        raw_email = msg_data[0][1]
        msg = email.message_from_bytes(raw_email)
        results.append((mid, msg))
    return results


def mark_read(conn, msg_ids):
    """批量标记邮件为已读"""
    for mid in msg_ids:
        conn.store(mid, "+FLAGS", "\\Seen")


# ── Email Parsing ────────────────────────────────────────────────────────


def get_email_body(msg):
    """从 email.Message 提取 HTML 正文"""
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

    # 策略 1: <h3><a href="...">Title</a></h3> + 后续文本作为 snippet
    for h3 in soup.find_all("h3"):
        a = h3.find("a", href=True)
        if not a:
            continue
        title = a.get_text(strip=True)
        url = a["href"]
        if not title or url in seen:
            continue
        seen.add(url)

        # 尝试获取 h3 后面的摘要文本
        snippet = ""
        node = h3.next_sibling
        parts = []
        while node and len(parts) < 200:  # 最多取 200 个字符
            if node.name == "h3":
                break
            text = node.get_text(strip=True) if hasattr(node, "get_text") else str(node).strip()
            if text:
                parts.append(text)
            node = node.next_sibling
        snippet = " ".join(parts)[:500]

        papers.append({"title": title, "url": url, "snippet": snippet})

    # 策略 2: 如果策略 1 没找到，尝试所有 <a> 标签（有些邮件格式不同）
    if not papers:
        for a in soup.find_all("a", href=True):
            href = a["href"]
            title = a.get_text(strip=True)
            # 过滤掉明显的导航链接
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
    """对单篇论文评分 + 翻译，返回 {score, reason, title_cn, abstract_cn?}"""
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
        # 清理可能的 markdown 代码块
        if content.startswith("```"):
            content = re.sub(r"^```\w*\n?", "", content)
            content = re.sub(r"\n```$", "", content)
        return json.loads(content)
    except Exception as e:
        print(f"  [ERROR] LLM 调用失败: {e}")
        return {"score": 0, "reason": f"LLM error: {e}", "title_cn": paper["title"]}


def mock_results(papers):
    """生成模拟结果（无 LLM API key 时使用）"""
    results = {"interesting": [], "boring": []}
    for i, p in enumerate(papers):
        # ponytail: 简单模拟 — 第1、2篇算感兴趣，第3篇不感兴趣
        if i < 2:
            results["interesting"].append({
                "title": p["title"], "url": p["url"], "snippet": p["snippet"],
                "score": 8 - i,
                "reason": "与用户研究方向高度相关" if i == 0 else "涉及多智能体系统",
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


def process_papers(papers, dry_run=False):
    """处理论文列表，返回 {interesting: [...], boring: [...]}"""
    if dry_run:
        print(f"[DRY RUN] 将处理 {len(papers)} 篇论文，跳过 LLM 调用")
        return {"interesting": [], "boring": []}

    client = build_llm_client()
    interesting, boring = [], []

    for i, p in enumerate(papers):
        print(f"  [{i+1}/{len(papers)}] {p['title'][:60]}...")
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
  <p>由 AI 自动生成于 {date} | 兴趣配置见 <a href="https://github.com/{repo}/blob/main/config.yaml">config.yaml</a></p>
  <p>历史摘要: <a href="digest-{yesterday}.html">← 昨天</a> | 归档命名: digest-YYYY-MM-DD.html</p>
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


def build_repo_name():
    """从 GITHUB_REPOSITORY 环境变量获取仓库名"""
    return os.environ.get("GITHUB_REPOSITORY", "user/repo")


def generate_html(results, output_dir):
    """生成 index.html 和 digest-{date}.html"""
    output_dir.mkdir(parents=True, exist_ok=True)
    yesterday = (datetime.now(TZ) - timedelta(days=1)).strftime("%Y-%m-%d")

    interesting_html = render_papers(results["interesting"])
    boring_html = render_papers(results["boring"], cls="boring")
    repo = build_repo_name()

    html = HTML_TEMPLATE.format(
        date=TODAY,
        total=len(results["interesting"]) + len(results["boring"]),
        n_interesting=len(results["interesting"]),
        n_boring=len(results["boring"]),
        interesting_html=interesting_html,
        boring_html=boring_html,
        yesterday=yesterday,
        repo=repo,
    )

    # 写两份：index.html（首页）和 digest-{date}.html（归档）
    (output_dir / "index.html").write_text(html, encoding="utf-8")
    (output_dir / f"digest-{TODAY}.html").write_text(html, encoding="utf-8")
    print(f"生成 HTML: {output_dir}/index.html, {output_dir}/digest-{TODAY}.html")


# ── Main ─────────────────────────────────────────────────────────────────

def main():
    dry_run = "--dry-run" in sys.argv
    print(f"{'[DRY RUN] ' if dry_run else ''}论文整理 Agent — {TODAY}")
    print(f"兴趣阈值: >= {MIN_SCORE}/10")

    if dry_run:
        # 跳过邮箱，用模拟论文测试 LLM 流程
        mock_papers = [
            {"title": "Training Large Language Models with Reinforcement Learning from Human Feedback",
             "url": "https://arxiv.org/abs/example1", "snippet": "We present a method for aligning LLMs using RLHF to create helpful and harmless AI assistants..."},
            {"title": "A Survey on Multi-Agent Systems: From Game Theory to Deep Learning",
             "url": "https://arxiv.org/abs/example2", "snippet": "This paper surveys recent advances in multi-agent systems, covering cooperative and competitive settings..."},
            {"title": "Quantum Computing Applications in Cryptography: A Review",
             "url": "https://arxiv.org/abs/example3", "snippet": "We review applications of quantum computing to modern cryptography, including Shor's algorithm and post-quantum schemes..."},
        ]
        if not LLM_CFG.get("api_key"):
            print("未设置 LLM_API_KEY，使用模拟结果生成 HTML")
            results = mock_results(mock_papers)
        else:
            results = process_papers(mock_papers, dry_run=False)
        generate_html(results, OUTPUT_DIR)
        return

    # 1. 连接邮箱
    print("连接邮箱...")
    conn = connect_imap()
    try:
        # 2. 获取邮件
        emails = fetch_scholar_emails(conn)
        print(f"找到 {len(emails)} 封未读谷歌学术邮件")

        if not emails:
            print("无新邮件，生成空报告")
            generate_html({"interesting": [], "boring": []}, OUTPUT_DIR)
            return

        # 3. 提取论文
        all_papers = []
        for mid, msg in emails:
            body = get_email_body(msg)
            papers = extract_papers(body)
            print(f"  邮件 [{msg['Subject']}] → {len(papers)} 篇论文")
            all_papers.extend(papers)

        if not all_papers:
            print("未提取到论文，生成空报告")
            generate_html({"interesting": [], "boring": []}, OUTPUT_DIR)
            return

        # 去重
        seen = set()
        unique = []
        for p in all_papers:
            if p["url"] not in seen:
                seen.add(p["url"])
                unique.append(p)
        print(f"去重后共 {len(unique)} 篇论文")

        # 4. LLM 评分 + 翻译
        results = process_papers(unique[:MAX_PAPERS], dry_run=False)

        # 5. 生成 HTML
        generate_html(results, OUTPUT_DIR)

        # 6. 标记已读
        mark_read(conn, [mid for mid, _ in emails])
        print("已标记邮件为已读")
    finally:
        conn.logout()


if __name__ == "__main__":
    main()
