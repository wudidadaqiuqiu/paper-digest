#!/usr/bin/env python3
"""从 output/index.html 提取论文摘要，输出人类可读文本"""
from pathlib import Path
from bs4 import BeautifulSoup

SCRIPT_DIR = Path(__file__).resolve().parent
html_path = SCRIPT_DIR / "output" / "index.html"
soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")

# 标题行
h1 = soup.find("h1")
count = soup.find("p", class_="count")
if h1:
    print(f"## {h1.get_text(strip=True)}")
if count:
    print(f"{count.get_text(strip=True)}")
print()

from bs4 import Tag, NavigableString

for section in soup.find_all("h2"):
    print(f"### {section.get_text(strip=True)}")
    print()
    node = section.next_sibling
    count = 0
    while node:
        if isinstance(node, Tag) and node.name == "h2":
            break
        if isinstance(node, Tag) and "paper" in node.get("class", []):
            count += 1
            score = node.find("span", class_="score")
            title_en = node.find("a")
            meta = node.find("div", class_="meta")
            reason = node.find("div", class_="reason")
            abstract = node.find("div", class_="abstract")

            score = score.get_text(strip=True) if score else "?"
            title_en = title_en.get_text(strip=True) if title_en else ""
            meta = meta.get_text(strip=True) if meta else ""
            reason = reason.get_text(strip=True) if reason else ""
            abstract = abstract.get_text(strip=True) if abstract else ""

            print(f"{count}. [{score}] {title_en}")
            print(f"   CN: {meta}")
            print(f"   理由: {reason}")
            if abstract:
                print(f"   摘要: {abstract}")
            print()
        node = node.next_sibling
