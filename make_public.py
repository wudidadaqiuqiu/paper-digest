#!/usr/bin/env python3
import urllib.request, json

TOKEN = open("/home/ubuntu/code/feishu-ws/paper-digest/.token").read().strip()

def api(method, path, body=None):
    url = f"https://api.github.com/repos/wudidadaqiuqiu/paper-digest{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data,
        headers={"Authorization": f"token {TOKEN}", "Content-Type": "application/json"}, method=method)
    try:
        resp = urllib.request.urlopen(req)
        return resp.read().decode() or "OK"
    except urllib.error.HTTPError as e:
        return json.loads(e.read())

# 1. Set repo to public
print("Setting repo to public...")
r = api("PATCH", "", {"private": False})
print(f"  {r}")

# 2. Enable Pages
print("Enabling GitHub Pages...")
r = api("POST", "/pages", {"source": {"branch": "gh-pages", "path": "/"}})
print(f"  {r}")

# 3. Trigger workflow dispatch
print("Triggering workflow...")
r = api("POST", "/actions/workflows/daily-digest.yml/dispatches", {"ref": "main"})
print(f"  {r}")

print("\nDone! https://wudidadaqiuqiu.github.io/paper-digest/")
