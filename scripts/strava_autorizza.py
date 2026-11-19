"""
strava_autorizza.py — collega Strava una volta sola
────────────────────────────────────────────────────
Prima metti in scripts/.env:
    STRAVA_CLIENT_ID=...
    STRAVA_CLIENT_SECRET=...
(li trovi su https://www.strava.com/settings/api; "Authorization Callback
Domain" deve essere: localhost)

Poi lancia questo script: apre Strava nel browser, premi Autorizza e il
refresh token viene scritto in scripts/.env come STRAVA_REFRESH_TOKEN.
Non stampa mai i token.
"""

import http.server
import os
import sys
import urllib.parse
import webbrowser
from pathlib import Path

import requests

ENV = Path(__file__).parent / ".env"
PORT = 8765
SCOPES = "read,activity:read_all,activity:write,profile:read_all"


def read_env():
    values = {}
    for line in ENV.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip()
    return values


def write_env_value(key, value):
    lines = ENV.read_text(encoding="utf-8").splitlines()
    lines = [l for l in lines if not l.strip().startswith(key + "=")]
    lines.append(f"{key}={value}")
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    env = read_env()
    client_id, secret = env.get("STRAVA_CLIENT_ID"), env.get("STRAVA_CLIENT_SECRET")
    if not client_id or not secret:
        sys.exit("Mancano STRAVA_CLIENT_ID e STRAVA_CLIENT_SECRET in scripts/.env")

    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result["code"] = (query.get("code") or [None])[0]
            result["scope"] = (query.get("scope") or [""])[0]
            result["error"] = (query.get("error") or [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = "Fatto, puoi chiudere questa pagina." if result["code"] else "Autorizzazione non riuscita."
            self.wfile.write(f"<h2 style='font-family:sans-serif'>{msg}</h2>".encode())

        def log_message(self, *args):
            pass

    url = "https://www.strava.com/oauth/authorize?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": f"http://localhost:{PORT}/",
        "response_type": "code",
        "approval_prompt": "force",
        "scope": SCOPES,
    })
    server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    print("Apro Strava nel browser: premi Autorizza (lascia spuntate tutte le caselle).")
    webbrowser.open(url)
    server.timeout = 300
    server.handle_request()
    server.server_close()

    if not result.get("code"):
        sys.exit(f"Autorizzazione non riuscita: {result.get('error') or 'nessuna risposta entro 5 minuti'}")
    missing = [s for s in SCOPES.split(",") if s not in result.get("scope", "").split(",")]
    if missing:
        sys.exit(f"Mancano dei permessi: {', '.join(missing)}. Rilancia e lascia spuntate tutte le caselle.")

    r = requests.post("https://www.strava.com/oauth/token", data={
        "client_id": client_id,
        "client_secret": secret,
        "code": result["code"],
        "grant_type": "authorization_code",
    }, timeout=30)
    if not r.ok:
        sys.exit(f"Strava ha rifiutato il codice ({r.status_code})")
    data = r.json()
    write_env_value("STRAVA_REFRESH_TOKEN", data["refresh_token"])
    athlete = data.get("athlete") or {}
    print(f"Strava collegato (atleta {athlete.get('id')}). Token salvato in scripts/.env.")


if __name__ == "__main__":
    main()
