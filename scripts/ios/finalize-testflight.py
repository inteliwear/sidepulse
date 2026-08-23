#!/usr/bin/env python3
"""Wait for a build, attach it to an internal group, and add the account holder."""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import jwt


API = "https://api.appstoreconnect.apple.com"


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable {name}")
    return value


def make_token() -> str:
    with open(required("ASC_KEY_PATH"), "rb") as handle:
        private_key = handle.read()
    now = int(time.time())
    return jwt.encode(
        {
            "iss": required("ASC_ISSUER_ID"),
            "iat": now,
            "exp": now + 20 * 60,
            "aud": "appstoreconnect-v1",
        },
        private_key,
        algorithm="ES256",
        headers={"kid": required("ASC_KEY_ID"), "typ": "JWT"},
    )


def request(method: str, path: str, token: str, body: dict | None = None) -> tuple[int, dict]:
    payload = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{API}{path}", data=payload, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as response:
            text = response.read().decode() or "{}"
            return response.status, json.loads(text)
    except urllib.error.HTTPError as error:
        text = error.read().decode() or "{}"
        parsed = json.loads(text) if text.lstrip().startswith("{") else {"raw": text}
        return error.code, parsed


def query(path: str, parameters: dict[str, str]) -> str:
    return f"{path}?{urllib.parse.urlencode(parameters)}"


def expect(status: int, body: dict, allowed: tuple[int, ...], operation: str) -> dict:
    if status not in allowed:
        raise RuntimeError(f"{operation} failed ({status}): {body}")
    return body


def find_app(token: str, bundle_id: str) -> str:
    status, body = request("GET", query("/v1/apps", {"filter[bundleId]": bundle_id, "limit": "10"}), token)
    expect(status, body, (200,), "Find app")
    apps = body.get("data", [])
    if not apps:
        raise RuntimeError(f"No App Store Connect app found for {bundle_id}")
    return apps[0]["id"]


def wait_for_build(token: str, app_id: str, build_number: str) -> dict:
    path = query(
        "/v1/builds",
        {
            "filter[app]": app_id,
            "filter[version]": build_number,
            "sort": "-uploadedDate",
            "limit": "10",
        },
    )
    for attempt in range(60):
        status, body = request("GET", path, token)
        expect(status, body, (200,), "Find uploaded build")
        builds = body.get("data", [])
        if builds:
            build = builds[0]
            state = build.get("attributes", {}).get("processingState", "UNKNOWN")
            print(f"Build {build_number} processing state: {state}")
            if state == "VALID":
                return build
            if state in ("FAILED", "INVALID"):
                raise RuntimeError(f"Build {build_number} processing failed with state {state}")
        else:
            print(f"Build {build_number} has not appeared in App Store Connect yet.")
        if attempt < 59:
            time.sleep(30)
    raise RuntimeError(f"Timed out waiting for build {build_number} to finish processing")


def find_or_create_group(token: str, app_id: str, name: str) -> dict:
    path = query(
        "/v1/betaGroups",
        {"filter[app]": app_id, "filter[isInternalGroup]": "true", "limit": "200"},
    )
    status, body = request("GET", path, token)
    expect(status, body, (200,), "List internal groups")
    for group in body.get("data", []):
        if group.get("attributes", {}).get("name") == name:
            return group

    payload = {
        "data": {
            "type": "betaGroups",
            "attributes": {"name": name, "isInternalGroup": True},
            "relationships": {"app": {"data": {"type": "apps", "id": app_id}}},
        }
    }
    status, body = request("POST", "/v1/betaGroups", token, payload)
    return expect(status, body, (200, 201), "Create internal group")["data"]


def link(token: str, path: str, resource_type: str, resource_id: str, operation: str) -> None:
    status, body = request(
        "POST",
        path,
        token,
        {"data": [{"type": resource_type, "id": resource_id}]},
    )
    expect(status, body, (200, 201, 204, 409), operation)


def account_holder(token: str) -> dict:
    status, body = request("GET", query("/v1/users", {"limit": "200"}), token)
    expect(status, body, (200,), "List App Store Connect users")
    users = body.get("data", [])
    for preferred_role in ("ACCOUNT_HOLDER", "ADMIN"):
        for user in users:
            if preferred_role in user.get("attributes", {}).get("roles", []):
                return user
    if users:
        return users[0]
    raise RuntimeError("No App Store Connect user is available for internal testing")


def find_or_create_tester(token: str, user: dict) -> dict:
    attributes = user.get("attributes", {})
    email = attributes.get("username", "").strip()
    if not email:
        raise RuntimeError("The selected App Store Connect user has no email address")
    status, body = request(
        "GET",
        query("/v1/betaTesters", {"filter[email]": email, "limit": "10"}),
        token,
    )
    expect(status, body, (200,), "Find internal tester")
    if body.get("data"):
        return body["data"][0]

    payload = {
        "data": {
            "type": "betaTesters",
            "attributes": {
                "firstName": attributes.get("firstName") or "Internal",
                "lastName": attributes.get("lastName") or "Tester",
                "email": email,
            },
        }
    }
    status, body = request("POST", "/v1/betaTesters", token, payload)
    return expect(status, body, (200, 201), "Create internal tester")["data"]


def main() -> None:
    token = make_token()
    app_id = find_app(token, required("ASC_BUNDLE_ID"))
    build = wait_for_build(token, app_id, required("ASC_BUILD_NUMBER"))
    group = find_or_create_group(token, app_id, required("ASC_INTERNAL_GROUP"))
    link(
        token,
        f"/v1/betaGroups/{group['id']}/relationships/builds",
        "builds",
        build["id"],
        "Add build to internal group",
    )
    tester = find_or_create_tester(token, account_holder(token))
    link(
        token,
        f"/v1/betaGroups/{group['id']}/relationships/betaTesters",
        "betaTesters",
        tester["id"],
        "Add account holder to internal group",
    )
    print(
        json.dumps(
            {
                "appId": app_id,
                "buildId": build["id"],
                "buildNumber": required("ASC_BUILD_NUMBER"),
                "group": group.get("attributes", {}).get("name"),
                "processingState": build.get("attributes", {}).get("processingState"),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
