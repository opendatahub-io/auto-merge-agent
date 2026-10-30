"""Exchange the GitHub Actions OIDC token for a short-lived Quay robot token.

Runs in the publish workflow, whose job carries ``id-token: write`` so the
runner injects the token-request environment. Logs the non-secret OIDC claims
so mismatches against the Quay federation binding are visible in the job log,
then logs in to Quay. Exits non-zero with the response body on failure.
"""
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

QUAY_TOKEN_URL = "https://quay.io/oauth2/federation/robot/token"


def request(url: str, headers: dict[str, str]) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def fetch_oidc_token() -> str:
    url = urllib.parse.urlsplit(os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"])
    query = urllib.parse.parse_qsl(url.query) + [("audience", "quay")]
    token_url = url._replace(query=urllib.parse.urlencode(query)).geturl()
    status, body = request(token_url, {"Authorization": "bearer " + os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]})
    if status != 200:
        sys.exit(f"OIDC token request failed with HTTP {status}: {body.decode(errors='replace')}")
    token = json.loads(body)["value"]
    assert token, body
    print("::add-mask::" + token)
    return token


def main() -> None:
    user = os.environ["QUAY_ROBOT_USER"]
    oidc_token = fetch_oidc_token()
    payload = oidc_token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    print("iss:", claims.get("iss"))
    print("sub:", claims.get("sub"))
    print("aud:", claims.get("aud"))

    credentials = base64.b64encode(f"{user}:{oidc_token}".encode()).decode()
    status, body = request(QUAY_TOKEN_URL, {"Authorization": "Basic " + credentials})
    if status != 200:
        sys.exit(f"Quay token exchange failed with HTTP {status}: {body.decode(errors='replace')}")
    quay_token = json.loads(body)["token"]
    assert quay_token, body
    print("::add-mask::" + quay_token)
    subprocess.run(
        ["docker", "login", "quay.io", "--username", user, "--password-stdin"],
        input=quay_token, text=True, check=True,
    )


if __name__ == "__main__":
    main()
