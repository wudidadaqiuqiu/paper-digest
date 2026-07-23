#!/usr/bin/env python3
"""Set GitHub Actions secrets via API"""
import urllib.request, json, base64
from nacl import encoding, public

TOKEN = open("/home/ubuntu/code/feishu-ws/paper-digest/.token").read().strip()
OWNER, REPO = "wudidadaqiuqiu", "paper-digest"

def encrypt_secret(public_key_b64, secret_value):
    """GitHub uses libsodium sealed box, not RSA."""
    pk = public.PublicKey(public_key_b64.encode(), encoding.Base64Encoder())
    sealed = public.SealedBox(pk)
    encrypted = sealed.encrypt(secret_value.encode())
    return base64.b64encode(encrypted).decode()

def set_secret(name, value):
    url = f"https://api.github.com/repos/{OWNER}/{REPO}/actions/secrets/public-key"
    req = urllib.request.Request(url, headers={"Authorization": f"token {TOKEN}"})
    resp = json.loads(urllib.request.urlopen(req).read())

    body = json.dumps({
        "encrypted_value": encrypt_secret(resp["key"], value),
        "key_id": resp["key_id"],
    }).encode()
    req = urllib.request.Request(
        f"https://api.github.com/repos/{OWNER}/{REPO}/actions/secrets/{name}",
        data=body,
        headers={"Authorization": f"token {TOKEN}", "Content-Type": "application/json"},
        method="PUT",
    )
    urllib.request.urlopen(req)
    print(f"  {name} OK")

print("Setting secrets on wudidadaqiuqiu/paper-digest...")
set_secret("QQ_PASSWORD", "gagmioopnanccjcc")
set_secret("LLM_API_KEY", "sk-94840382aef74bdc804bb86768a5a360")
print("Done!")
