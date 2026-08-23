#!/usr/bin/env python3
"""
One-off bootstrap: register the iOS bundle ID with Apple Developer Portal and
create the App Store Connect app record. Idempotent — re-running is safe and
just reports the existing IDs.

Required env vars:
  ASC_KEY_ID       — 10-char App Store Connect API key id (e.g. 2MS26MK9TX)
  ASC_ISSUER_ID    — App Store Connect API issuer UUID
  ASC_KEY_PATH     — path to the .p8 file
  ASC_BUNDLE_ID    — bundle id to register (e.g. com.example.sidepulse)
  ASC_APP_NAME     — App Store name (≤30 chars, e.g. "Voice Assist")
  ASC_APP_SKU      — unique SKU within your account (e.g. sidepulse-ios)
  ASC_PRIMARY_LOC  — primary locale (default en-US)
"""
import base64
import json
import os
import sys
import time
import urllib.request
import urllib.error

try:
    import jwt
except ImportError:
    print("ERROR: pip install pyjwt cryptography", file=sys.stderr)
    sys.exit(1)


API = "https://api.appstoreconnect.apple.com"


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if not value:
        print(f"ERROR: env var {name} required", file=sys.stderr)
        sys.exit(2)
    return value


def make_token(key_id: str, issuer_id: str, key_path: str) -> str:
    with open(key_path, "rb") as fh:
        private_key = fh.read()
    now = int(time.time())
    payload = {
        "iss": issuer_id,
        "iat": now,
        "exp": now + 20 * 60,
        "aud": "appstoreconnect-v1",
    }
    return jwt.encode(payload, private_key, algorithm="ES256", headers={"kid": key_id, "typ": "JWT"})


def request(method: str, path: str, token: str, body: dict | None = None) -> tuple[int, dict]:
    url = f"{API}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            payload = resp.read().decode() or "{}"
            return resp.status, json.loads(payload)
    except urllib.error.HTTPError as err:
        payload = err.read().decode() or "{}"
        return err.code, json.loads(payload) if payload.strip().startswith("{") else {"raw": payload}


def find_bundle_id(token: str, bundle_id: str) -> str | None:
    status, body = request("GET", f"/v1/bundleIds?filter[identifier]={bundle_id}&limit=200", token)
    if status != 200:
        print(f"List bundleIds failed: {status} {body}", file=sys.stderr)
        sys.exit(3)
    for item in body.get("data", []):
        attrs = item.get("attributes", {})
        if attrs.get("identifier") == bundle_id and attrs.get("platform") in ("IOS", "UNIVERSAL"):
            return item["id"]
    return None


def create_bundle_id(token: str, bundle_id: str, name: str) -> str:
    print(f"Registering bundle id {bundle_id} as iOS…")
    payload = {
        "data": {
            "type": "bundleIds",
            "attributes": {
                "identifier": bundle_id,
                "name": name,
                "platform": "IOS",
            }
        }
    }
    status, body = request("POST", "/v1/bundleIds", token, payload)
    if status not in (200, 201):
        print(f"Create bundleId failed: {status} {body}", file=sys.stderr)
        sys.exit(4)
    return body["data"]["id"]


def find_app(token: str, bundle_id: str) -> str | None:
    status, body = request("GET", f"/v1/apps?filter[bundleId]={bundle_id}&limit=200", token)
    if status != 200:
        print(f"List apps failed: {status} {body}", file=sys.stderr)
        sys.exit(5)
    data = body.get("data", [])
    return data[0]["id"] if data else None


def create_app(token: str, bundle_id_resource: str, name: str, sku: str, locale: str) -> str:
    print(f"Creating ASC app record '{name}'…")
    payload = {
        "data": {
            "type": "apps",
            "attributes": {
                "bundleId": bundle_id_resource,  # ignored on POST but harmless
                "name": name,
                "primaryLocale": locale,
                "sku": sku,
            },
            "relationships": {
                "bundleId": {"data": {"type": "bundleIds", "id": bundle_id_resource}},
            }
        }
    }
    status, body = request("POST", "/v1/apps", token, payload)
    if status not in (200, 201):
        print(f"Create app failed: {status} {body}", file=sys.stderr)
        sys.exit(6)
    return body["data"]["id"]


def main() -> None:
    key_id = env("ASC_KEY_ID")
    issuer_id = env("ASC_ISSUER_ID")
    key_path = env("ASC_KEY_PATH")
    bundle_id = env("ASC_BUNDLE_ID")
    name = env("ASC_APP_NAME")
    sku = env("ASC_APP_SKU")
    locale = env("ASC_PRIMARY_LOC", "en-US")

    token = make_token(key_id, issuer_id, key_path)

    bundle_resource = find_bundle_id(token, bundle_id)
    if bundle_resource:
        print(f"Bundle id {bundle_id} already registered (resource {bundle_resource}).")
    else:
        bundle_resource = create_bundle_id(token, bundle_id, name)
        print(f"Created bundle id resource {bundle_resource}.")

    app_id = find_app(token, bundle_id)
    if app_id:
        print(f"App for {bundle_id} already exists (id {app_id}). Nothing to do.")
        return
    app_id = create_app(token, bundle_resource, name, sku, locale)
    print(f"Created app id {app_id}. Add internal testers in App Store Connect to invite them via TestFlight.")


if __name__ == "__main__":
    main()
