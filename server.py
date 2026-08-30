#!/usr/bin/env python3
"""Paper Digest HTTP 服务 — 导航 + 手动触发处理 + 进度轮询"""

import json
import base64
import threading
import re
import os
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

import main as digest

SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = SCRIPT_DIR / "output"
SERVER_CFG = digest.cfg.get("server", {})
PORT = SERVER_CFG.get("port", 8080)
PASSWORD = SERVER_CFG.get("password", "admin")
AUTH_B64 = base64.b64encode(f"admin:{PASSWORD}".encode()).decode()

# 全局进度状态
_progress = {"stage": "idle", "current": 0, "total": 0, "message": ""}
_lock = threading.Lock()

# ── Nav Page HTML ────────────────────────────────────────────────────────

NAV_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Paper Digest</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; background: #fafafa; color: #333; }
  h1 { font-size: 1.4em; border-bottom: 2px solid #2563eb; padding-bottom: 8px; margin-bottom: 20px; }
  .actions { margin: 16px 0; display: flex; gap: 10px; align-items: center; }
  button { padding: 8px 20px; background: #2563eb; color: #fff; border: none; border-radius: 6px; cursor: pointer; font-size: .95em; }
  button:hover { background: #1d4ed8; }
  button:disabled { background: #94a3b8; cursor: not-allowed; }
  #progress { display: none; margin: 12px 0; padding: 12px 16px; background: #fff; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
  #progress.visible { display: block; }
  .bar { height: 6px; background: #e2e8f0; border-radius: 3px; margin-top: 8px; overflow: hidden; }
  .bar-fill { height: 100%%; background: #2563eb; width: 0%%; transition: width .3s; border-radius: 3px; }
  .status { font-size: .85em; color: #666; }
  .digest-list { margin-top: 24px; }
  .digest-list h2 { font-size: 1.1em; color: #666; margin-bottom: 12px; }
  .digest-list a { display: block; padding: 8px 12px; color: #2563eb; text-decoration: none; border-radius: 6px; }
  .digest-list a:hover { background: #e8f0fe; }
  .digest-list .date { color: #999; font-size: .85em; margin-left: 8px; }
  .empty { color: #999; font-style: italic; padding: 12px; }
  .error { color: #dc2626; font-size: .85em; }
</style>
</head>
<body>
<h1>📄 Paper Digest</h1>

<div class="actions">
  <button id="btn-process" onclick="startProcess()">处理新论文</button>
  <span id="error" class="error"></span>
</div>

<div id="progress">
  <div class="status" id="status-msg"></div>
  <div class="bar"><div class="bar-fill" id="bar-fill"></div></div>
</div>

<div class="digest-list">
  <h2>📋 历史摘要</h2>
  <div id="digest-list">{digest_links}</div>
</div>

<script>
let polling = null;

async function startProcess() {
  const btn = document.getElementById('btn-process');
  const err = document.getElementById('error');
  const prog = document.getElementById('progress');
  err.textContent = '';
  btn.disabled = true;
  prog.classList.add('visible');
  document.getElementById('status-msg').textContent = '启动中...';

  try {
    const resp = await fetch('/process', { method: 'POST' });
    if (!resp.ok) { throw new Error(await resp.text()); }
    poll();
  } catch (e) {
    err.textContent = e.message;
    btn.disabled = false;
    prog.classList.remove('visible');
  }
}

async function poll() {
  if (polling) return;
  polling = setInterval(async () => {
    try {
      const resp = await fetch('/progress');
      const p = await resp.json();
      document.getElementById('status-msg').textContent = p.message || p.stage;
      const bar = document.getElementById('bar-fill');
      if (p.total > 0) bar.style.width = (p.current / p.total * 100) + '%';
      else if (p.stage === 'done') bar.style.width = '100%';
      if (p.stage === 'done' || p.stage === 'error') {
        clearInterval(polling);
        polling = null;
        document.getElementById('btn-process').disabled = false;
        if (p.stage === 'done') location.reload();
      }
    } catch (e) {
      clearInterval(polling);
      polling = null;
      document.getElementById('btn-process').disabled = false;
    }
  }, 1000);
}

// 页面加载时检查是否正在处理
(async () => {
  const resp = await fetch('/progress');
  const p = await resp.json();
  if (p.stage !== 'idle' && p.stage !== 'done') {
    document.getElementById('progress').classList.add('visible');
    document.getElementById('btn-process').disabled = true;
    poll();
  }
})();
</script>
</body>
</html>"""


def build_digest_links():
    """扫描 output/ 生成 digest 链接列表 HTML"""
    if not OUTPUT_DIR.exists():
        return '<div class="empty">暂无摘要</div>'

    files = sorted(OUTPUT_DIR.glob("digest-*.html"), reverse=True)
    if not files:
        return '<div class="empty">暂无摘要</div>'

    index_link = '<a href="/index.html">📋 最新摘要 <span class="date">index</span></a>'
    parts = [index_link]
    for f in files:
        date_str = f.stem.replace("digest-", "")
        parts.append(f'<a href="/{f.name}">📄 {date_str} <span class="date">{date_str}</span></a>')
    return "\n".join(parts)


# ── HTTP Handler ─────────────────────────────────────────────────────────


class Handler(BaseHTTPRequestHandler):
    timeout = 30  # 单连接超时，防止恶意/慢客户端阻塞

    def do_AUTH(self):
        """检查 Basic Auth，失败返回 401"""
        auth = self.headers.get("Authorization", "")
        if auth == f"Basic {AUTH_B64}":
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Paper Digest"')
        self.end_headers()
        self.wfile.write(b"Unauthorized")
        return False

    def do_GET(self):
        if not self.do_AUTH():
            return

        path = urlparse(self.path).path

        if path == "/":
            self.serve_nav()
        elif path == "/progress":
            self.serve_progress()
        elif path.startswith("/digest-") or path == "/index.html":
            self.serve_static(path.lstrip("/"))
        else:
            self.send_error(404)

    def do_POST(self):
        if not self.do_AUTH():
            return

        if self.path == "/process":
            self.handle_process()
        else:
            self.send_error(404)

    def serve_nav(self):
        html = NAV_PAGE.replace("{digest_links}", build_digest_links())
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(data))
        self.end_headers()
        self.wfile.write(data)

    def serve_progress(self):
        with _lock:
            data = json.dumps(_progress, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(data))
        self.end_headers()
        self.wfile.write(data)

    def serve_static(self, filename):
        # 安全检查：拒绝路径穿越
        safe = re.sub(r"[^a-zA-Z0-9_.-]", "", filename)
        filepath = OUTPUT_DIR / safe
        if not filepath.exists() or not filepath.is_file():
            self.send_error(404)
            return

        data = filepath.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(data))
        self.end_headers()
        self.wfile.write(data)

    def handle_process(self):
        """启动后台处理线程"""
        with _lock:
            if _progress["stage"] not in ("idle", "done", "error"):
                self.send_response(409)
                self.end_headers()
                self.wfile.write(b"Already processing")
                return
            _progress["stage"] = "connecting"
            _progress["current"] = 0
            _progress["total"] = 0
            _progress["message"] = "启动中..."

        def progress_cb(stage, current, total, message):
            with _lock:
                _progress["stage"] = stage
                _progress["current"] = current
                _progress["total"] = total
                _progress["message"] = message

        def runner():
            try:
                digest.run_pipeline(progress_callback=progress_cb)
            except Exception as e:
                with _lock:
                    _progress["stage"] = "error"
                    _progress["message"] = f"失败: {e}"

        t = threading.Thread(target=runner, daemon=True)
        t.start()

        self.send_response(202)
        self.end_headers()
        self.wfile.write(b"Accepted")

    def log_message(self, format, *args):
        print(f"[server] {args[0]}")


# ── Main ─────────────────────────────────────────────────────────────────


def main():
    print(f"Paper Digest 服务启动: http://0.0.0.0:{PORT}")
    print(f"用户名: admin")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True  # 线程随主进程退出，不阻塞关闭
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        server.shutdown()


if __name__ == "__main__":
    main()
