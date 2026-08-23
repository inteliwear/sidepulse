#!/usr/bin/env python3
"""
Create an iOS distribution certificate via the App Store Connect API.

Required env vars:
  ASC_KEY_ID, ASC_ISSUER_ID, ASC_KEY_PATH — App Store Connect API auth
  ASC_CERTIFICATE_CSR_PATH                — PEM CSR path to submit

Optional env vars:
  ASC_CERTIFICATE_TYPE                    — defaults to DISTRIBUTION

Writes a JSON object to stdout containing the certificate id and base64
certificateContent returned by App Store Connect.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

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


def list_certificates(token: str, certificate_type: str) -> list[dict]:
    status, body = request(
        "GET",
        f"/v1/certificates?filter[certificateType]={certificate_type}&limit=200",
        token,
    )
    if status != 200:
        print(f"List certificates failed: {status} {body}", file=sys.stderr)
        sys.exit(3)
    return body.get("data", [])


def revoke_certificate(token: str, certificate_id: str) -> None:
    status, body = request("DELETE", f"/v1/certificates/{certificate_id}", token)
    if status not in (200, 204):
        print(f"Revoke certificate {certificate_id} failed: {status} {body}", file=sys.stderr)
        sys.exit(3)
    print(f"Revoked existing certificate {certificate_id}.", file=sys.stderr)


def create_certificate(token: str, certificate_type: str, csr_content: str) -> dict:
    payload = {
        "data": {
            "type": "certificates",
            "attributes": {
                "certificateType": certificate_type,
                "csrContent": csr_content,
            },
        }
    }
    status, body = request("POST", "/v1/certificates", token, payload)
    if status == 409:
        # Apple caps to one active cert per certificateType per team. Revoke
        # everything of this type and retry. The CSR private key is generated
        # fresh each run, so the old cert is useless to us anyway.
        print("Active certificate(s) blocking new one — revoking and retrying.", file=sys.stderr)
        for cert in list_certificates(token, certificate_type):
            revoke_certificate(token, cert["id"])
        status, body = request("POST", "/v1/certificates", token, payload)
    if status not in (200, 201):
        print(f"Create certificate failed: {status} {body}", file=sys.stderr)
        sys.exit(3)
    return body["data"]


def fetch_certificate(token: str, certificate_id: str) -> dict:
    path = (
        f"/v1/certificates/{certificate_id}"
        "?fields[certificates]=name,displayName,serialNumber,certificateType,expirationDate,certificateContent"
    )
    status, body = request("GET", path, token)
    if status != 200:
        print(f"Fetch certificate failed: {status} {body}", file=sys.stderr)
        sys.exit(4)
    return body["data"]


def main() -> None:
    key_id = env("ASC_KEY_ID")
    issuer_id = env("ASC_ISSUER_ID")
    key_path = env("ASC_KEY_PATH")
    csr_path = env("ASC_CERTIFICATE_CSR_PATH")
    certificate_type = env("ASC_CERTIFICATE_TYPE", "DISTRIBUTION")

    with open(csr_path, "r", encoding="utf-8") as fh:
        csr_content = fh.read().strip()

    token = make_token(key_id, issuer_id, key_path)
    created = create_certificate(token, certificate_type, csr_content)
    certificate_id = created["id"]
    certificate = fetch_certificate(token, certificate_id)
    attrs = certificate.get("attributes", {})

    result = {
        "id": certificate_id,
        "certificateType": attrs.get("certificateType"),
        "displayName": attrs.get("displayName"),
        "name": attrs.get("name"),
        "serialNumber": attrs.get("serialNumber"),
        "expirationDate": attrs.get("expirationDate"),
        "certificateContent": attrs.get("certificateContent"),
    }
    if not result["certificateContent"]:
        print(f"Certificate {certificate_id} did not include certificateContent.", file=sys.stderr)
        sys.exit(5)

    print(f"Created certificate {certificate_id} ({result['displayName']}).", file=sys.stderr)
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()
