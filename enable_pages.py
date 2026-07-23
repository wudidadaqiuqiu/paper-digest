#!/usr/bin/env python3
"""Enable GitHub Pages for the repo"""
import urllib.request, json

TOKEN = open("/home/ubuntu/code/feishu-ws/paper-digest/.token").read().strip()
OWNER, REPO = "wudidadaqiuqiu", "paper-digest"

def api(method, path, body=None):
    url = f"https://api.github.com/repos/{OWNER}/{REPO}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data,
        headers={"Authorization": f"token {TOKEN}", "Content-Type": "application/json"}, method=method)
    try:
        resp = urllib.request.urlopen(req)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())

# 1. Enable Pages on gh-pages branch
print("Enabling GitHub Pages...")
result = api("POST", "/pages", {"source": {"branch": "gh-pages", "path": "/"}})
print(f"  Pages: {result.get('html_url', result.get('message', result))}")

# 2. Trigger workflow to create gh-pages
print("Triggering first workflow run...")
result = api("POST", "/actions/workflows/daily-digest.yml/dispatches", {"ref": "main"})
print(f"  Workflow: {result}")
print("\nDone! Pages URL: https://wudidadaqiuqiu.github.io/paper-digest/")
