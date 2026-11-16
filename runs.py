"""Show recent runs from one provider or check the local running history API."""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime
import hashlib
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import webbrowser

GARMIN = "https://personal.connectapi.garmin.com"
STRAVA = "https://www.strava.com"
HEALTH_PORT = 8001
PROVIDER_TIMEOUT = 20
HEALTH_TIMEOUT = 5
GARMIN_RECONNECT = "Replace GARMIN_PERSONAL_API_KEY in Garmin Connect and update your settings."
STRAVA_RECONNECT = "Check your Strava app settings and run python3 runs.py strava --connect."


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
        raise ConnectionProblem(f"Credential setup failed. Set {name} in .env and retry.")
    return value


class NoRedirect(HTTPRedirectHandler):
    # The probe must contact only the selected provider, including on redirects.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def retry_advice(header):
    value = (header or "").strip()
    if re.fullmatch(r"[0-9]{1,10}", value):
        return f"Retry after {value} seconds."
    try:
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is not None:
            return f"Retry at {format_datetime(retry_at.astimezone(timezone.utc), usegmt=True)}."
    except (ValueError, TypeError, OverflowError):
        pass
    return "Wait for the rate limit to reset and retry."


def request(url, *, step, reconnect, headers=None, form=None, method=None, plain=False):
    body = urlencode(form).encode() if form is not None else None
    headers = {"Accept": "*/*" if plain else "application/json", **(headers or {})}
    if any(not value.isascii() or not value.isprintable() for value in headers.values()):
        raise ConnectionProblem(f"{step}: invalid credential format. {reconnect}")
    req = Request(url, data=body, headers=headers, method=method)
    try:
        with build_opener(NoRedirect()).open(req, timeout=PROVIDER_TIMEOUT) as response:
            return response.read().decode().strip() if plain else json.load(response)
    except HTTPError as err:
        with err:
            if err.code == 429:
                reason = "Rate limit reached. " + retry_advice(err.headers.get("Retry-After"))
            elif err.code in (400, 401, 403):
                reason = {400: "Request rejected.", 401: "Credential rejected.", 403: "Access denied."}[err.code]
                reason += " " + reconnect
            else:
                reason = "Unexpected response. Check provider availability and retry."
            raise ProviderError(err.code, f"{step}: HTTP {err.code}. {reason}") from None
    except (OSError, HTTPException) as err:
        cause = err.reason if isinstance(err, URLError) else err
        if isinstance(cause, TimeoutError):
            raise ConnectionProblem(f"{step} timed out. Check your connection and retry.") from None
        raise ConnectionProblem(f"{step} failed. Check your network connection and retry.") from None
    except (ValueError, RecursionError):
        raise ConnectionProblem(f"{step}: unexpected response. Check provider availability and retry.") from None


def activity_page(url, token, params, *, step, reconnect, id_field):
    page = request(url + "?" + urlencode(params), headers={"Authorization": "Bearer " + token},
                   step=step, reconnect=reconnect)
    if not isinstance(page, list) or any(
        not isinstance(a, dict) or not re.fullmatch(r"[0-9]+", str(a.get(id_field)))
        for a in page
    ):
        raise ConnectionProblem(f"{step}: unexpected activity list. Check provider availability and retry.")
    return page


def valid_token(value):
    return isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9._~+/-]+=*", value) is not None


def garmin_runs(settings, limit):
    key = required(settings, "GARMIN_PERSONAL_API_KEY")
    params = {"activityType": "running", "sortOrder": "desc", "start": 0, "limit": limit}
    for attempt in range(2):
        token = request(GARMIN + "/personalapitoken-service/personal-api/personal-api-key/token",
                        step="Garmin token exchange", reconnect=GARMIN_RECONNECT,
                        method="POST", headers={"Authorization": "ApiKey " + key}, plain=True)
        if not valid_token(token):
            raise ConnectionProblem("Garmin token exchange: unexpected token response. Check Garmin availability and retry.")
        try:
            return activity_page(GARMIN + "/activitylist-service/activities/search/activities", token, params,
                                 step="Garmin activity search", reconnect=GARMIN_RECONNECT, id_field="activityId")[:limit]
        except ProviderError as err:
            if err.status != 401:
                raise
            if attempt == 1:
                raise ConnectionProblem(f"Garmin activity search: HTTP 401 after a new bearer token. {GARMIN_RECONNECT}") from None


def strava_code(client_id, redirect):
    callback_help = "Strava sign-in: set STRAVA_REDIRECT_URI to a local HTTP callback such as http://localhost:8000/callback."
    try:
        address = urlsplit(redirect)
        port = address.port
    except ValueError:
        raise ConnectionProblem(callback_help) from None
    if (address.scheme != "http" or address.hostname not in ("localhost", "127.0.0.1")
            or address.username is not None or address.query or address.fragment or port == 0):
        raise ConnectionProblem(callback_help)
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
                result["error"] = "Strava sign-in declined. Run with --connect again to grant activity access."
            elif not {"activity:read", "activity:read_all"}.intersection(params.get("scope", [""])[0].replace(",", " ").split()):
                result["error"] = "Strava sign-in: allow activity read access and retry --connect."
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
    try:
        server = HTTPServer(("127.0.0.1", port or 80), Callback)
    except OSError:
        raise ConnectionProblem("Strava sign-in: could not open the local callback port. Free the port and retry --connect.") from None
    with server:
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
        try:
            grant = json.loads(value)
        except (ValueError, RecursionError):
            grant = None
        if (not isinstance(grant, dict)
                or any(grant.get(k) is not None and not isinstance(grant[k], str)
                       for k in ("access_token", "refresh_token"))
                or (grant.get("expires_at") is not None and type(grant["expires_at"]) is not int)):
            raise ConnectionProblem("Strava credential setup: invalid saved grant. Run strava --connect to replace it.")
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
        raise ConnectionProblem("Strava credential setup: set STRAVA_CLIENT_ID to your numeric Strava application ID.")
    directory = Path.home() / ".config" / "adaptive-marathon-plan"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / f"strava-{client_id}.token"

    access = settings.get("STRAVA_ACCESS_TOKEN")
    refresh = settings.get("STRAVA_REFRESH_TOKEN")
    seed = refresh or access
    fingerprint = hashlib.sha256(seed.encode()).hexdigest() if seed else None
    with (directory / f"strava-{client_id}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not connect:
            grant = read_grant(path)
            # New .env credentials select a new grant. Otherwise keep Strava's latest rotation.
            if seed and grant.get("seed") != fingerprint:
                grant = {"access_token": access, "refresh_token": refresh}
            current = grant.get("access_token")
            expiry = grant.get("expires_at")
            if current and current != rejected and (expiry is None or expiry > time.time() + 60):
                return current
            if not grant.get("refresh_token"):
                if rejected is not None:
                    raise ConnectionProblem(f"Strava activity list: HTTP 401. No refresh token is available. {STRAVA_RECONNECT}")
                raise ConnectionProblem("Strava credential setup: set STRAVA_REFRESH_TOKEN with activity read access in .env "
                                        "or run: python3 runs.py strava --connect")
            payload = {"grant_type": "refresh_token", "refresh_token": grant["refresh_token"]}
            fingerprint = fingerprint or grant.get("seed")
        client_secret = required(settings, "STRAVA_CLIENT_SECRET")
        if connect:
            code = strava_code(client_id, required(settings, "STRAVA_REDIRECT_URI"))
            payload = {"grant_type": "authorization_code", "code": code}
        step = "Strava sign-in token exchange" if connect else "Strava token refresh"
        reply = request(STRAVA + "/oauth/token", step=step, reconnect=STRAVA_RECONNECT, form={
            "client_id": client_id, "client_secret": client_secret, **payload,
        })
        if (not isinstance(reply, dict) or not valid_token(reply.get("access_token"))
                or not isinstance(reply.get("refresh_token"), str) or not reply["refresh_token"].strip()
                or type(reply.get("expires_at")) is not int):
            raise ConnectionProblem(f"{step}: unexpected token response. {STRAVA_RECONNECT}")
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
            raise ConnectionProblem("Strava activity list: HTTP 401 after refresh. The athlete token "
                                    "needs activity:read or activity:read_all. Run with --connect to grant access.") from None
        raise


def strava_pages(token, limit):
    runs = []
    page_number = 1
    seen_ids = set()
    while len(runs) < limit:
        page = activity_page(STRAVA + "/api/v3/athlete/activities", token,
                             {"page": page_number, "per_page": 30}, step="Strava activity list",
                             reconnect=STRAVA_RECONNECT, id_field="id")
        if not page:
            break
        previous_count = len(seen_ids)
        for activity in page:
            sport = activity.get("sport_type") or activity.get("type")
            if not isinstance(sport, str):
                raise ConnectionProblem("Strava activity list: unexpected sport type. Check Strava availability and retry.")
            ident = str(activity["id"])
            if ident not in seen_ids and sport in ("Run", "TrailRun", "VirtualRun"):
                runs.append(activity)
            seen_ids.add(ident)
        if len(seen_ids) == previous_count:
            raise ConnectionProblem("Strava activity list: repeated page. Check Strava availability and retry.")
        page_number += 1
    return runs[:limit]


def activity_row(provider, activity):
    if provider == "garmin":
        start, seconds, name, ident = (activity.get(k) for k in ("startTimeLocal", "elapsedDuration", "activityName", "activityId"))
        base = "https://connect.garmin.com/modern/activity/"
    else:
        start, seconds, name, ident = (activity.get(k) for k in ("start_date_local", "elapsed_time", "name", "id"))
        base = STRAVA + "/activities/"
    metres = activity.get("distance")
    for value in (metres, seconds):
        if value is not None and (type(value) not in (int, float) or value < 0 or not math.isfinite(value)):
            raise ValueError
    if any(value is not None and not isinstance(value, str) for value in (name, start)):
        raise ValueError
    km = f"{metres / 1000:.2f} km" if metres is not None else "—"
    duration = str(timedelta(seconds=round(seconds))) if seconds is not None else "—"
    date = "—"
    if start:
        local = datetime.fromisoformat(start.replace("Z", "+00:00"))
        date = local.replace(tzinfo=None).isoformat(" ", timespec="seconds")
    title = "".join(char if char.isprintable() else " " for char in (name or "Untitled"))
    return f"{date:19}  {km:>8}  {duration:>8}   {title}  {base}{ident}"


def display(provider, activities):
    try:
        rows = [activity_row(provider, activity) for activity in activities]
    except (ValueError, OverflowError):
        raise ConnectionProblem(f"{provider.title()} activity list: unexpected activity fields. Check provider availability and retry.") from None
    print(f"{provider.title()} connected. {len(rows)} recent run(s).")
    if rows:
        print("Local start            Distance   Elapsed   Activity")
        print(*rows, sep="\n")
    else:
        print("No visible runs found.")


def check_health():
    # A direct loopback connection cannot follow redirects or use a proxy.
    with closing(HTTPConnection("127.0.0.1", HEALTH_PORT, timeout=HEALTH_TIMEOUT)) as connection:
        try:
            connection.request("GET", "/health")
            with connection.getresponse() as response:
                if response.status == 503:
                    raise ConnectionProblem("Local health: HTTP 503. Check RUNS_DB_PATH and SQLite read permissions, then retry.")
                if response.status != 200:
                    raise ConnectionProblem(f"Local health: HTTP {response.status}. Check the FastAPI server and retry.")
                if json.load(response) != {"status": "ok"}:
                    raise ValueError
        except TimeoutError:
            raise ConnectionProblem("Local health timed out. Check that FastAPI is responding and retry.") from None
        except (OSError, HTTPException):
            raise ConnectionProblem(f"Local health connection failed. Start FastAPI on 127.0.0.1:{HEALTH_PORT} and retry.") from None
        except (ValueError, RecursionError):
            raise ConnectionProblem("Local health: unexpected response. Check the FastAPI server and retry.") from None
    print("Local API is healthy. SQLite is readable.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", choices=("garmin", "strava", "health"))
    parser.add_argument("--limit", type=int, metavar="1-10")
    parser.add_argument("--connect", action="store_true", help="Authorise Strava once in your browser, or reconnect.")
    args = parser.parse_args()
    if args.connect and args.provider != "strava":
        parser.error("--connect is for Strava")
    if args.limit is not None and args.provider == "health":
        parser.error("--limit is for Garmin or Strava")
    limit = 10 if args.limit is None else args.limit
    if not 1 <= limit <= 10:
        parser.error("--limit must be between 1 and 10")
    try:
        if args.provider == "health":
            check_health()
            return 0
        try:
            settings = load_settings()
        except (OSError, ValueError):
            raise ConnectionProblem(f"{args.provider.title()} credential setup: could not read .env. Check its encoding and permissions and retry.") from None
        if args.provider == "strava":
            try:
                activities = strava_runs(settings, limit, args.connect)
            except (OSError, UnicodeError):
                raise ConnectionProblem("Strava credential storage: check read/write permissions and retry --connect.") from None
        else:
            activities = garmin_runs(settings, limit)
        display(args.provider, activities)
    except ConnectionProblem as err:
        print(err, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
