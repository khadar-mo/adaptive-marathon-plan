"""Show recent runs from one personal account. Requires only Python 3."""
import argparse
from datetime import timedelta
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
import os
from pathlib import Path
import secrets
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen
import webbrowser

GARMIN = "https://personal.connectapi.garmin.com"
STRAVA = "https://www.strava.com"


class ConnectionProblem(Exception):
    pass


class ProviderError(ConnectionProblem):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def load_settings():
    values = {}
    path = Path(__file__).with_name(".env")
    if path.exists():
        for line in path.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and not line.lstrip().startswith("#"):
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                values[key.strip()] = value
    values.update(os.environ)
    return values


def required(settings, name):
    value = settings.get(name)
    if not value:
        raise ConnectionProblem(f"Set {name} in .env and run the command again.")
    return value


def request(url, *, headers=None, form=None, method=None, plain=False):
    body = urlencode(form).encode() if form is not None else None
    headers = {"Accept": "*/*" if plain else "application/json", **(headers or {})}
    req = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(req, timeout=20) as response:
            return response.read().decode().strip() if plain else json.load(response)
    except HTTPError as err:
        reason = {400: "Check credentials and callback settings.",
                  401: "Check the credential or reconnect.",
                  403: "The account did not grant access.",
                  429: "Rate limit reached. Try again after the quota resets."}
        raise ProviderError(err.code, f"HTTP {err.code} at {urlsplit(url).path}. "
                                + reason.get(err.code, "The provider could not complete the request.")) from err
    except (URLError, TimeoutError, ValueError) as err:
        raise ConnectionProblem(f"Unable to read {urlsplit(url).hostname} ({type(err).__name__}).") from None


def activity_page(url, token, params):
    page = request(url + "?" + urlencode(params), headers={"Authorization": "Bearer " + token})
    if not isinstance(page, list) or any(not isinstance(a, dict) for a in page):
        raise ConnectionProblem("The provider returned an unexpected activity list.")
    return page


def garmin_runs(settings, limit):
    key = required(settings, "GARMIN_PERSONAL_API_KEY")
    token = request(GARMIN + "/personalapitoken-service/personal-api/personal-api-key/token",
                    method="POST", headers={"Authorization": "ApiKey " + key}, plain=True)
    if not token:
        raise ConnectionProblem("Garmin returned an empty token.")
    return activity_page(GARMIN + "/activitylist-service/activities/search/activities", token,
                         {"activityType": "running", "sortOrder": "desc", "start": 0, "limit": limit})[:limit]


def strava_code(client_id, redirect):
    address = urlsplit(redirect)
    if (address.scheme != "http" or address.hostname not in ("localhost", "127.0.0.1")
            or address.username or address.query or address.fragment):
        raise ConnectionProblem("Set STRAVA_REDIRECT_URI to a local HTTP callback such as http://localhost:8000/callback.")
    state = secrets.token_urlsafe(32)
    result = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Callback URLs contain a one-time code.

        def do_GET(self):
            url = urlsplit(self.path)
            if url.path != (address.path or "/"):
                self.send_error(404)
                return
            params = parse_qs(url.query)
            if params.get("state") != [state]:
                self.send_error(400, "Unexpected OAuth state")
                return
            if params.get("error") or not params.get("code"):
                result["error"] = "Strava authorisation was declined."
            elif not {"activity:read", "activity:read_all"}.intersection(params.get("scope", [""])[0].replace(",", " ").split()):
                result["error"] = "Allow activity read access in Strava and try again."
            else:
                result["code"] = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b"Return to the terminal to see your result.")

    auth_url = STRAVA + "/oauth/authorize?" + urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code",
        "scope": "activity:read_all", "state": state,
    })
    with HTTPServer(("127.0.0.1", address.port or 80), Callback) as server:
        print("Approve read access in your browser. If it did not open, visit:", auth_url,
              sep="\n", file=sys.stderr, flush=True)
        webbrowser.open(auth_url)
        deadline = time.monotonic() + 180
        while not result and time.monotonic() < deadline:
            server.timeout = max(0.01, deadline - time.monotonic())
            server.handle_request()
    if "code" not in result:
        raise ConnectionProblem(result.get("error", "Strava sign-in timed out. Run the command again."))
    return result["code"]


def read_grant(path):
    if not path.exists():
        return {}
    value = path.read_text().strip()
    if value.startswith("{"):
        grant = json.loads(value)
        if not isinstance(grant, dict):
            raise ConnectionProblem("Invalid local Strava credentials. Run with --connect to replace them.")
        return grant
    return {"refresh_token": value}  # Earlier installs saved only the refresh token.


def save_grant(path, grant):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as tmp:
        try:
            json.dump(grant, tmp)
            tmp.flush()
            os.fsync(tmp.fileno())
            os.replace(tmp.name, path)
        finally:
            Path(tmp.name).unlink(missing_ok=True)


def strava_token(settings, connect=False, rejected=None):
    import fcntl  # Local CLI on macOS/Linux. Serialise refresh-token rotation.
    client_id = required(settings, "STRAVA_CLIENT_ID")
    if not client_id.isascii() or not client_id.isdigit():
        raise ConnectionProblem("STRAVA_CLIENT_ID must be a numeric Strava application ID.")
    directory = Path.home() / ".config" / "adaptive-marathon-plan"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / f"strava-{client_id}.token"

    access = settings.get("STRAVA_ACCESS_TOKEN")
    refresh = settings.get("STRAVA_REFRESH_TOKEN")
    seed = refresh or access
    fingerprint = hashlib.sha256(seed.encode()).hexdigest() if seed else None
    with (directory / f"strava-{client_id}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if connect:
            code = strava_code(client_id, required(settings, "STRAVA_REDIRECT_URI"))
            payload = {"grant_type": "authorization_code", "code": code}
        else:
            grant = read_grant(path)
            # New .env credentials select a new grant. Otherwise keep Strava's latest rotation.
            if seed and grant.get("seed") != fingerprint:
                grant = {"access_token": access, "refresh_token": refresh}
            current = grant.get("access_token")
            expiry = grant.get("expires_at")
            if current and current != rejected and (expiry is None or expiry > time.time() + 60):
                return current
            if not grant.get("refresh_token"):
                raise ConnectionProblem("Set STRAVA_REFRESH_TOKEN with activity read access in .env "
                                        "or run: python3 runs.py strava --connect")
            payload = {"grant_type": "refresh_token", "refresh_token": grant["refresh_token"]}
            fingerprint = fingerprint or grant.get("seed")
        try:
            reply = request(STRAVA + "/oauth/token", form={
                "client_id": client_id, "client_secret": required(settings, "STRAVA_CLIENT_SECRET"), **payload,
            })
        except ProviderError as err:
            if not connect and err.status in (400, 401):
                raise ConnectionProblem(f"{err} Check STRAVA_REFRESH_TOKEN or reconnect with --connect.") from None
            raise
        if (not isinstance(reply, dict) or not all(isinstance(reply.get(k), str) and reply[k]
                for k in ("access_token", "refresh_token")) or not isinstance(reply.get("expires_at"), int)):
            raise ConnectionProblem("Strava returned an incomplete token response.")
        saved = {key: reply[key] for key in ("access_token", "refresh_token", "expires_at")}
        save_grant(path, {**saved, "seed": fingerprint})
        return saved["access_token"]


def strava_runs(settings, limit, connect=False):
    token = strava_token(settings, connect)
    try:
        return strava_pages(token, limit)
    except ProviderError as err:
        if err.status != 401:
            raise
    token = strava_token(settings, rejected=token)
    try:
        return strava_pages(token, limit)
    except ProviderError as err:
        if err.status == 401:
            raise ConnectionProblem("Strava rejected activity access after refresh. The athlete token "
                                    "needs activity:read or activity:read_all. Run with --connect to grant access.") from None
        raise


def strava_pages(token, limit):
    runs = []
    page_number = 1
    while len(runs) < limit:
        page = activity_page(STRAVA + "/api/v3/athlete/activities", token,
                             {"page": page_number, "per_page": 30})
        if not page:
            break
        runs.extend(a for a in page if (a.get("sport_type") or a.get("type")) in {"Run", "TrailRun", "VirtualRun"})
        page_number += 1
    return runs[:limit]


def display(provider, activities):
    print(f"{provider.title()} connected. {len(activities)} recent run(s).")
    if not activities:
        print("No visible runs found.")
        return
    print("Local start         Distance   Elapsed   Activity")
    for a in activities:
        if provider == "garmin":
            start, seconds, name, ident = (a.get(k) for k in ("startTimeLocal", "elapsedDuration", "activityName", "activityId"))
            base = "https://connect.garmin.com/modern/activity/"
        else:
            start, seconds, name, ident = (a.get(k) for k in ("start_date_local", "elapsed_time", "name", "id"))
            base = STRAVA + "/activities/"
        metres = a.get("distance")
        km = f"{metres / 1000:.2f} km" if isinstance(metres, (int, float)) and math.isfinite(metres) else "—"
        duration = str(timedelta(seconds=round(seconds))) if isinstance(seconds, (int, float)) and math.isfinite(seconds) else "—"
        date = str(start or "—").replace("T", " ")[:16]
        title = "".join(char if char.isprintable() else " " for char in str(name or "Untitled"))
        link = base + str(ident) if str(ident).isdigit() else ""
        print(f"{date:16}  {km:>8}  {duration:>8}   {title}  {link}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", choices=("garmin", "strava"))
    parser.add_argument("--limit", type=int, default=10, metavar="1-10")
    parser.add_argument("--connect", action="store_true", help="Authorise Strava once in your browser, or reconnect.")
    args = parser.parse_args()
    if args.connect and args.provider != "strava":
        parser.error("--connect is for Strava")
    if not 1 <= args.limit <= 10:
        parser.error("--limit must be between 1 and 10")
    try:
        settings = load_settings()
        if args.provider == "strava":
            activities = strava_runs(settings, args.limit, args.connect)
        else:
            activities = garmin_runs(settings, args.limit)
        display(args.provider, activities)
    except (ConnectionProblem, OSError, ValueError) as err:
        print(f"{args.provider.title()}: {err}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
