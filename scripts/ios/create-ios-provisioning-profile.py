#!/usr/bin/env python3
"""
Create an iOS App Store provisioning profile via the App Store Connect API.

Idempotent: if a profile with the configured name already exists, returns it.
Writes the base64-encoded .mobileprovision content to stdout so the caller can
pipe it into `gh secret set IOS_MOBILE_PROVISION`.

Required env vars:
  ASC_KEY_ID, ASC_ISSUER_ID, ASC_KEY_PATH — App Store Connect API auth
  ASC_BUNDLE_ID                          — bundle identifier
  ASC_PROFILE_NAME                       — desired profile name (idempotent key)
  ASC_PROFILE_TYPE                       — IOS_APP_STORE | IOS_APP_DEVELOPMENT

Optional env vars:
  ASC_CERTIFICATE_IDS                    — comma-separated certificate ids to
                                           bind to the profile. If omitted,
                                           all current DISTRIBUTION certs are
                                           used as before.
  ASC_REGISTER_MISSING_BUNDLE_ID         — "true" to register ASC_BUNDLE_ID
                                           before creating the profile.
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
    payload = {"iss": issuer_id, "iat": now, "exp": now + 20 * 60, "aud": "appstoreconnect-v1"}
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


def find_profile(token: str, name: str) -> dict | None:
    # The API doesn't expose a name filter, so list and match.
    status, body = request("GET", "/v1/profiles?limit=200&include=bundleId,certificates", token)
    if status != 200:
        print(f"List profiles failed: {status} {body}", file=sys.stderr)
        sys.exit(3)
    for item in body.get("data", []):
        if item.get("attributes", {}).get("name") == name:
            return item
    return None


def find_bundle_id_resource(token: str, bundle_id: str) -> str | None:
    status, body = request("GET", f"/v1/bundleIds?filter[identifier]={bundle_id}&limit=200", token)
    if status != 200:
        print(f"List bundleIds failed: {status} {body}", file=sys.stderr)
        sys.exit(4)
    for item in body.get("data", []):
        attrs = item.get("attributes", {})
        if attrs.get("identifier") == bundle_id and attrs.get("platform") in ("IOS", "UNIVERSAL"):
            return item["id"]
    return None


def create_bundle_id(token: str, bundle_id: str, name: str) -> str:
    payload = {
        "data": {
            "type": "bundleIds",
            "attributes": {
                "identifier": bundle_id,
                "name": name,
                "platform": "IOS",
            },
        }
    }
    status, body = request("POST", "/v1/bundleIds", token, payload)
    if status not in (200, 201):
        print(f"Create bundleId failed: {status} {body}", file=sys.stderr)
        sys.exit(11)
    return body["data"]["id"]


def find_distribution_certificate_ids(token: str) -> list[str]:
    # Only the modern DISTRIBUTION (Apple Distribution) cert type — including
    # IOS_DISTRIBUTION (legacy) makes xcodebuild look for an iPhone-Dist cert
    # we don't have locally, breaking exportArchive.
    status, body = request(
        "GET",
        "/v1/certificates?filter[certificateType]=DISTRIBUTION&limit=200",
        token,
    )
    if status != 200:
        print(f"List certificates failed: {status} {body}", file=sys.stderr)
        sys.exit(5)
    return [item["id"] for item in body.get("data", [])]


def delete_profile(token: str, profile_id: str) -> None:
    status, body = request("DELETE", f"/v1/profiles/{profile_id}", token)
    if status not in (200, 204):
        print(f"Delete profile failed: {status} {body}", file=sys.stderr)
        sys.exit(10)


def create_profile(
    token: str, name: str, profile_type: str, bundle_id_resource: str, cert_ids: list[str]
) -> dict:
    payload = {
        "data": {
            "type": "profiles",
            "attributes": {"name": name, "profileType": profile_type},
            "relationships": {
                "bundleId": {"data": {"type": "bundleIds", "id": bundle_id_resource}},
                "certificates": {"data": [{"type": "certificates", "id": cid} for cid in cert_ids]},
            },
        }
    }
    status, body = request("POST", "/v1/profiles", token, payload)
    if status not in (200, 201):
        print(f"Create profile failed: {status} {body}", file=sys.stderr)
        sys.exit(6)
    return body["data"]


def fetch_profile_content(token: str, profile_id: str) -> str:
    status, body = request("GET", f"/v1/profiles/{profile_id}?fields[profiles]=profileContent", token)
    if status != 200:
        print(f"Fetch profile content failed: {status} {body}", file=sys.stderr)
        sys.exit(7)
    return body["data"]["attributes"]["profileContent"]


def main() -> None:
    key_id = env("ASC_KEY_ID")
    issuer_id = env("ASC_ISSUER_ID")
    key_path = env("ASC_KEY_PATH")
    bundle_id = env("ASC_BUNDLE_ID")
    profile_name = env("ASC_PROFILE_NAME")
    profile_type = env("ASC_PROFILE_TYPE", "IOS_APP_STORE")
    explicit_cert_ids = os.environ.get("ASC_CERTIFICATE_IDS", "").strip()
    register_missing_bundle_id = os.environ.get("ASC_REGISTER_MISSING_BUNDLE_ID", "").lower() == "true"

    token = make_token(key_id, issuer_id, key_path)

    existing = find_profile(token, profile_name)
    if existing:
        print(f"Profile '{profile_name}' already exists ({existing['id']}); deleting and recreating.", file=sys.stderr)
        delete_profile(token, existing["id"])

    bundle_resource = find_bundle_id_resource(token, bundle_id)
    if not bundle_resource:
        if not register_missing_bundle_id:
            print(f"Bundle id {bundle_id} not registered for iOS — run create-ios-asc-app.py first.", file=sys.stderr)
            sys.exit(8)
        bundle_resource = create_bundle_id(token, bundle_id, profile_name)
        print(f"Created bundle id resource {bundle_resource} for {bundle_id}.", file=sys.stderr)

    if explicit_cert_ids:
        cert_ids = [cid.strip() for cid in explicit_cert_ids.split(",") if cid.strip()]
    else:
        cert_ids = find_distribution_certificate_ids(token)
    if not cert_ids:
        print("No iOS distribution certificates found in App Store Connect.", file=sys.stderr)
        sys.exit(9)
    print(f"Using {len(cert_ids)} distribution certificate(s).", file=sys.stderr)

    new_profile = create_profile(token, profile_name, profile_type, bundle_resource, cert_ids)
    print(f"Created profile {new_profile['id']}.", file=sys.stderr)
    content = fetch_profile_content(token, new_profile["id"])
    sys.stdout.write(content)


if __name__ == "__main__":
    main()
