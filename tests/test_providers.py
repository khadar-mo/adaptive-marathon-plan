import socket
import threading
from urllib.error import URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

import pytest

import runs


GARMIN_KEY = "synthetic-key"
ACCESS = "synthetic-access"
REFRESH = "synthetic-refresh"
CLIENT_SECRET = "synthetic-secret"
ERROR_BODY = b"private-provider-response"
TOKEN = "/garmin/personalapitoken-service/personal-api/personal-api-key/token"
SEARCH = "/garmin/activitylist-service/activities/search/activities"
STRAVA_LIST = "/strava/api/v3/athlete/activities"


def strava_credentials(monkeypatch):
    monkeypatch.setenv("STRAVA_CLIENT_ID", "123")
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setenv("STRAVA_ACCESS_TOKEN", ACCESS)
    monkeypatch.setenv("STRAVA_REFRESH_TOKEN", REFRESH)


def assert_failure(result, *messages):
    code, out, err = result
    assert code != 0
    for message in messages:
        assert message.lower() in err.lower()
    for private in (GARMIN_KEY, ACCESS, REFRESH, CLIENT_SECRET, ERROR_BODY.decode()):
        assert private not in out + err


def test_garmin_reacquires_once_and_displays_connect_fields(cli, upstream, monkeypatch):
    monkeypatch.setenv("GARMIN_PERSONAL_API_KEY", GARMIN_KEY)
    upstream.reply(b"first-token")
    upstream.reply(ERROR_BODY, 401)
    upstream.reply(b"second-token")
    upstream.reply([{
        "activityId": 42, "activityName": "Fixture", "startTimeLocal": "2026-11-01 00:15:38",
        "startTimeGMT": "2026-10-31 23:15:38", "distance": 5123.4,
        "elapsedDuration": 1800.6, "duration": 1500, "movingDuration": 1490,
    }])
    code, out, err = cli("garmin", "--limit", "1")
    assert code == 0 and not err
    for field in ("https://connect.garmin.com/modern/activity/42", "2026-11-01 00:15:38", "5.12 km", "0:30:01"):
        assert field in out
    query = "?activityType=running&sortOrder=desc&start=0&limit=1"
    assert upstream.calls == [
        ("POST", TOKEN, "ApiKey " + GARMIN_KEY, ""), ("GET", SEARCH + query, "Bearer first-token", ""),
        ("POST", TOKEN, "ApiKey " + GARMIN_KEY, ""), ("GET", SEARCH + query, "Bearer second-token", ""),
    ]
    assert all(value not in out for value in (GARMIN_KEY, "first-token", "second-token"))


@pytest.mark.parametrize("provider", ["garmin", "strava"])
def test_empty_list_is_success_and_contacts_only_selected_provider(provider, cli, upstream, monkeypatch):
    if provider == "garmin":
        monkeypatch.setenv("GARMIN_PERSONAL_API_KEY", GARMIN_KEY)
        upstream.reply(b"bearer")
    else:
        strava_credentials(monkeypatch)
    upstream.reply([])
    code, out, err = cli(provider, "--limit", "10")
    assert code == 0 and "No visible runs" in out and not err
    assert all(path.startswith("/" + provider + "/") for _, path, _, _ in upstream.calls)


@pytest.mark.parametrize("provider", ["garmin", "strava"])
def test_default_limit_is_ten(provider, cli, upstream, monkeypatch):
    if provider == "garmin":
        monkeypatch.setenv("GARMIN_PERSONAL_API_KEY", GARMIN_KEY)
        upstream.reply(b"bearer")
        upstream.reply([{"activityId": index} for index in range(1, 12)])
    else:
        strava_credentials(monkeypatch)
        upstream.reply([{"id": index, "type": "Run"} for index in range(1, 12)])
    code, out, err = cli(provider)
    assert code == 0 and not err
    assert "10 recent run(s)" in out and out.splitlines()[-1].endswith("/10")
    if provider == "garmin":
        assert parse_qs(urlsplit(upstream.calls[-1][1]).query)["limit"] == ["10"]


@pytest.mark.parametrize(("provider", "name"), [("garmin", "GARMIN_PERSONAL_API_KEY"), ("strava", "STRAVA_CLIENT_ID")])
def test_missing_settings_fail_before_http(provider, name, cli, upstream):
    assert_failure(cli(provider), "credential setup", "set " + name)
    assert not upstream.calls


def test_persistent_garmin_401_stops_after_one_retry(cli, upstream, monkeypatch):
    monkeypatch.setenv("GARMIN_PERSONAL_API_KEY", GARMIN_KEY)
    for _ in range(2):
        upstream.reply(b"bearer")
        upstream.reply(ERROR_BODY, 401)
    assert_failure(cli("garmin"), "activity search", "401", "new bearer", "replace GARMIN_PERSONAL_API_KEY")
    assert len(upstream.calls) == 4


@pytest.mark.parametrize(("status", "headers", "messages"), [
    (401, {}, ("401", "shell environment", "active status", "overrides .env")),
    (403, {}, ("403", "access denied", "active status")),
    (429, {"Retry-After": "60"}, ("rate limit", "60 seconds")),
    (429, {"Retry-After": "00000000060"}, ("retry after 60 seconds",)),
    (429, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, ("retry at Wed, 21 Oct 2026 07:28:00 GMT",)),
    (429, {"Retry-After": "Wed Oct 21 07:28:00 2026"}, ("retry at Wed, 21 Oct 2026 07:28:00 GMT",)),
    (429, {"Retry-After": CLIENT_SECRET}, ("rate limit", "reset", "retry")),
    (503, {}, ("503", "retry")),
])
def test_garmin_exchange_errors_name_recovery_without_retrying(status, headers, messages, cli, upstream, monkeypatch):
    monkeypatch.setenv("GARMIN_PERSONAL_API_KEY", GARMIN_KEY)
    upstream.reply(ERROR_BODY, status, headers)
    assert_failure(cli("garmin"), "Garmin token exchange", *messages)
    assert len(upstream.calls) == 1


@pytest.mark.parametrize(("line", "key"), [
    ("GARMIN_PERSONAL_API_KEY=synthetic-key # comment", GARMIN_KEY),
    ('export GARMIN_PERSONAL_API_KEY="synthetic-key" # comment', GARMIN_KEY),
    ("GARMIN_PERSONAL_API_KEY='fixture#key' # comment", "fixture#key"),
])
def test_garmin_key_from_file(line, key, cli, upstream, tmp_path):
    (tmp_path / ".env").write_text(line + "\n")
    upstream.reply(ERROR_BODY, 401)
    result = cli("garmin")
    assert_failure(result, "token exchange", "401", "from .env", "active status")
    assert key not in result[2] and upstream.calls[0][2] == "ApiKey " + key


def test_garmin_shell_override_is_trimmed_and_identified(cli, upstream, tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("GARMIN_PERSONAL_API_KEY=fixture-file\n")
    monkeypatch.setenv("GARMIN_PERSONAL_API_KEY", "  " + GARMIN_KEY + "\t")
    upstream.reply(ERROR_BODY, 401)
    result = cli("garmin")
    assert_failure(result, "from the shell environment", "overrides .env")
    assert "fixture-file" not in result[2] and upstream.calls[0][2] == "ApiKey " + GARMIN_KEY


@pytest.mark.parametrize("exported", ["", "   "])
def test_empty_shell_override_explains_recovery(cli, upstream, tmp_path, monkeypatch, exported):
    (tmp_path / ".env").write_text("GARMIN_PERSONAL_API_KEY=fixture-file\n")
    monkeypatch.setenv("GARMIN_PERSONAL_API_KEY", exported)
    assert_failure(cli("garmin"), "credential setup", "shell environment", "unset", "use .env")
    assert not upstream.calls
    monkeypatch.delenv("GARMIN_PERSONAL_API_KEY")
    upstream.reply(b"bearer")
    upstream.reply([])
    assert cli("garmin")[0] == 0


def test_comment_after_empty_file_setting_is_missing(cli, upstream, tmp_path):
    (tmp_path / ".env").write_text("GARMIN_PERSONAL_API_KEY= # fill in later\n")
    assert_failure(cli("garmin"), "credential setup", "set GARMIN_PERSONAL_API_KEY in .env")
    assert not upstream.calls


def test_garmin_pasted_header_is_rejected_before_network(cli, upstream, monkeypatch):
    monkeypatch.setenv("GARMIN_PERSONAL_API_KEY", "ApiKey " + GARMIN_KEY)
    assert_failure(cli("garmin"), "credential setup", "raw Personal API key")
    assert not upstream.calls


@pytest.mark.parametrize(("provider", "step"), [("garmin", "Garmin activity search"), ("strava", "Strava activity list")])
@pytest.mark.parametrize(("payload", "status", "messages"), [
    (ERROR_BODY, 403, ("access denied",)),
    (ERROR_BODY, 429, ("retry after 18 seconds",)),
    (ERROR_BODY, 200, ("unexpected response", "retry")),
    ({"error": CLIENT_SECRET}, 200, ("unexpected activity list", "retry")),
    ([{"message": CLIENT_SECRET}], 200, ("unexpected activity list", "retry")),
])
def test_activity_failures(provider, step, payload, status, messages, cli, upstream, monkeypatch):
    if provider == "garmin":
        monkeypatch.setenv("GARMIN_PERSONAL_API_KEY", GARMIN_KEY)
        upstream.reply(b"bearer")
    else:
        strava_credentials(monkeypatch)
    upstream.reply(payload, status, {"Retry-After": "18"})
    assert_failure(cli(provider), step, *messages)
    assert len(upstream.calls) == (2 if provider == "garmin" else 1)


@pytest.mark.parametrize("invalid", [b"", b"<html>sign in</html>", b'{"token":"synthetic-secret"}'])
def test_unexpected_garmin_token_never_used(invalid, cli, upstream, monkeypatch):
    monkeypatch.setenv("GARMIN_PERSONAL_API_KEY", GARMIN_KEY)
    upstream.reply(invalid)
    assert_failure(cli("garmin"), "token exchange", "unexpected", "retry")
    assert len(upstream.calls) == 1


@pytest.mark.parametrize("field", [
    {"startTimeLocal": CLIENT_SECRET}, {"startTimeLocal": "2026-11-01"}, {"distance": -1},
    {"elapsedDuration": 10**30}, {"activityName": {"token": CLIENT_SECRET}},
])
def test_invalid_activity_values_fail_without_partial_success(field, cli, upstream, monkeypatch):
    monkeypatch.setenv("GARMIN_PERSONAL_API_KEY", GARMIN_KEY)
    upstream.reply(b"bearer")
    upstream.reply([{"activityId": 1, **field}])
    result = cli("garmin")
    assert_failure(result, "activity list", "unexpected activity fields", "retry")
    assert not result[1]


def test_activity_with_only_minutes_preserves_recorded_precision(cli, upstream, monkeypatch):
    monkeypatch.setenv("GARMIN_PERSONAL_API_KEY", GARMIN_KEY)
    upstream.reply(b"bearer")
    upstream.reply([{"activityId": 1, "startTimeLocal": "2026-11-01T08:15+01:00"}])
    code, out, err = cli("garmin")
    assert code == 0 and not err
    assert "2026-11-01 08:15 " in out and "08:15:00" not in out


@pytest.mark.parametrize(("provider", "stage"), [("garmin", "Garmin token exchange"), ("strava", "Strava activity list")])
@pytest.mark.parametrize("error", [TimeoutError(CLIENT_SECRET), URLError(TimeoutError(CLIENT_SECRET))])
def test_timeout_does_not_echo_exception(provider, stage, error, cli, upstream, monkeypatch):
    monkeypatch.setenv("GARMIN_PERSONAL_API_KEY", GARMIN_KEY)
    strava_credentials(monkeypatch)

    def time_out(*args, **kwargs):
        raise error

    monkeypatch.setattr(runs, "build_opener", time_out)
    assert_failure(cli(provider), stage, "timed out", "retry")


def test_probe_refuses_provider_redirect(cli, upstream, monkeypatch):
    monkeypatch.setenv("GARMIN_PERSONAL_API_KEY", GARMIN_KEY)
    upstream.reply(ERROR_BODY, 302, {"Location": upstream.url + "/strava/leak"})
    assert_failure(cli("garmin"), "token exchange", "302", "retry")
    assert len(upstream.calls) == 1


@pytest.mark.parametrize("rejected", [False, True])
def test_strava_refreshes_after_401(cli, upstream, monkeypatch, rejected):
    strava_credentials(monkeypatch)
    upstream.reply(ERROR_BODY, 401)
    upstream.reply({"access_token": "new-access", "refresh_token": "new-refresh", "expires_at": 4102444800})
    if rejected:
        upstream.reply(ERROR_BODY, 401)
        assert_failure(cli("strava"), "activity list", "401", "--connect")
    else:
        upstream.reply([{"id": 1, "type": "Ride"}])
        upstream.reply([{"id": 7, "sport_type": "TrailRun", "start_date_local": "2026-01-01T08:12:32Z",
                         "distance": 1000, "elapsed_time": 280, "moving_time": 240}])
        code, out, err = cli("strava", "--limit", "1")
        assert code == 0 and not err
        assert all(value in out for value in ("08:12:32", "/activities/7", "1.00 km", "0:04:40"))
        assert all(value not in out for value in (ACCESS, REFRESH, "new-access", "new-refresh", CLIENT_SECRET))
        assert upstream.calls[3][1] == STRAVA_LIST + "?page=2&per_page=30"
    assert upstream.calls[0][2] == "Bearer " + ACCESS
    assert upstream.calls[1][:2] == ("POST", "/strava/oauth/token")
    assert parse_qs(upstream.calls[1][3])["refresh_token"] == [REFRESH]
    assert upstream.calls[2][2] == "Bearer new-access"


@pytest.mark.parametrize(("payload", "status", "messages"), [
    (ERROR_BODY, 400, ("400", "--connect")), (ERROR_BODY, 403, ("access denied", "--connect")),
    ({"error": CLIENT_SECRET}, 200, ("unexpected", "--connect")),
])
def test_strava_refresh_failure(payload, status, messages, cli, upstream, monkeypatch):
    strava_credentials(monkeypatch)
    monkeypatch.delenv("STRAVA_ACCESS_TOKEN")
    upstream.reply(payload, status)
    assert_failure(cli("strava"), "token refresh", *messages)


def test_strava_access_only_rejection_explains_how_to_reconnect(cli, upstream, monkeypatch):
    strava_credentials(monkeypatch)
    monkeypatch.delenv("STRAVA_REFRESH_TOKEN")
    upstream.reply(ERROR_BODY, 401)
    assert_failure(cli("strava"), "activity list", "401", "--connect")
    assert len(upstream.calls) == 1


def test_strava_repeated_nonrunning_page_stops(cli, upstream, monkeypatch):
    strava_credentials(monkeypatch)
    for _ in range(2):
        upstream.reply([{"id": 1, "type": "Ride"}])
    assert_failure(cli("strava"), "activity list", "repeated page", "retry")
    assert len(upstream.calls) == 2


def test_strava_connect_checks_credentials_before_opening_browser(cli, upstream, monkeypatch):
    monkeypatch.setenv("STRAVA_CLIENT_ID", "123")
    monkeypatch.setattr(runs.webbrowser, "open", lambda *args: pytest.fail("unexpected sign-in"))
    assert_failure(cli("strava", "--connect"), "credential setup", "set STRAVA_CLIENT_SECRET")
    assert not upstream.calls


def test_strava_connect_callback_grants_access_and_reuses_tokens(cli, upstream, monkeypatch):
    strava_credentials(monkeypatch)
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        callback = f"http://127.0.0.1:{available.getsockname()[1]}/callback"
    monkeypatch.setenv("STRAVA_REDIRECT_URI", callback)
    responses = []
    browser_threads = []

    def browser(url):
        query = parse_qs(urlsplit(url).query)
        assert query["redirect_uri"] == [callback] and query["scope"] == ["activity:read_all"]
        params = urlencode({"code": "synthetic-code", "scope": "activity:read_all", "state": query["state"][0]})

        def approve():
            with urlopen(callback + "?" + params, timeout=2) as response:
                responses.append(response.status)

        thread = threading.Thread(target=approve)
        browser_threads.append(thread)
        thread.start()
        return True

    monkeypatch.setattr(runs.webbrowser, "open", browser)
    upstream.reply({"access_token": "new-access", "refresh_token": "new-refresh", "expires_at": 4102444800})
    upstream.reply([])
    code, out, err = cli("strava", "--connect", "--limit", "1")
    browser_threads[0].join(timeout=3)
    assert code == 0 and responses == [200]
    assert all(v not in out + err for v in (CLIENT_SECRET, "synthetic-code", "new-access", "new-refresh"))
    assert parse_qs(upstream.calls[0][3]) == {
        "grant_type": ["authorization_code"], "code": ["synthetic-code"],
        "client_id": ["123"], "client_secret": [CLIENT_SECRET],
    }
    upstream.reply([])
    assert cli("strava")[0] == 0
    assert [c[2] for c in upstream.calls] == [None, "Bearer new-access", "Bearer new-access"]


@pytest.mark.parametrize("args", [("garmin", "--limit", "0"), ("strava", "--limit", "11"),
                                  ("health", "--limit", "1"), ("garmin", "--connect")])
def test_invalid_options_fail_before_http(args, cli, upstream):
    with pytest.raises(SystemExit) as result:
        cli(*args)
    assert result.value.code == 2 and not upstream.calls
