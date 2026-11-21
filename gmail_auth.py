#!/usr/bin/env python3
"""One-time Gmail sign-in for Mac Copilot: read and send (loopback OAuth + PKCE, stdlib only).

Needs google_client.json (a Google "Desktop app" OAuth client) next to this file.
Saves the refresh token to gmail_token.json (chmod 600, git-ignored).
"""
import base64
import hashlib
import http.server
import json
import os
import secrets
import subprocess
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CLIENT_FILE = os.path.join(HERE, "google_client.json")
TOKEN_FILE = os.path.join(HERE, "gmail_token.json")
SCOPES = ("https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.send "
          "https://www.googleapis.com/auth/calendar.readonly https://www.googleapis.com/auth/calendar.events")


def _login_hint():
    """Optional explicitly configured account hint for this OAuth flow."""
    return os.environ.get("JARVIS_GOOGLE_LOGIN_HINT", "")


def main():
    if not os.path.exists(CLIENT_FILE):
        raise SystemExit("google_client.json is missing next to gmail_auth.py.")
    with open(CLIENT_FILE) as f:
        client = json.load(f)["installed"]

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" not in q and "error" not in q:
                self.send_response(404)
                self.end_headers()
                return
            result.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>Jarvis: Google connected. You can close this tab.</h2>")

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect = "http://127.0.0.1:%d" % server.server_address[1]
    url = client["auth_uri"] + "?" + urllib.parse.urlencode({
        "client_id": client["client_id"],
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "login_hint": _login_hint(),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    })
    print("A Google sign-in page is opening in your browser. Pick your account and click Allow.")
    subprocess.run(["open", url])
    server.timeout = 300
    while "code" not in result and "error" not in result:
        server.handle_request()

    if "error" in result or result.get("state") != state:
        raise SystemExit("Sign-in failed: %s" % result.get("error", "state mismatch"))

    body = urllib.parse.urlencode({
        "code": result["code"],
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "redirect_uri": redirect,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }).encode()
    with urllib.request.urlopen(client["token_uri"], data=body) as r:
        tok = json.load(r)
    if "refresh_token" not in tok:
        raise SystemExit("No refresh token returned")
    if "gmail.send" not in tok.get("scope", ""):
        raise SystemExit("Signed in, but sending wasn't allowed. Run this again and tick every box.")

    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"refresh_token": tok["refresh_token"], "scope": tok.get("scope")}, f)

    req = urllib.request.Request("https://gmail.googleapis.com/gmail/v1/users/me/profile",
                                 headers={"Authorization": "Bearer " + tok["access_token"]})
    with urllib.request.urlopen(req) as r:
        prof = json.load(r)
    print("Connected:", prof.get("emailAddress"), "(read + send" + (" + calendar)" if "calendar.events" in tok.get("scope", "") else ")"))


if __name__ == "__main__":
    main()
