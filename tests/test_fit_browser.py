import os
import shutil
from datetime import datetime, timezone

import pytest

from fit_fixture import START, make_fit
from test_health import serve_database
from garmin_fit_sdk import convert_datetime_to_timestamp


def chrome_executable():
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    if not chrome and not os.environ.get("GITHUB_ACTIONS"):
        pytest.skip("Install Chrome or Chromium on PATH to run the browser replay")
    assert chrome, "The browser replay requires Chrome on the runner"
    return chrome


def test_upload_and_resolution_in_chrome(tmp_path):
    chrome = chrome_executable()
    from playwright.sync_api import expect, sync_playwright

    first = make_fit(laps=[(312.25, 80, 90)])
    def selected(name, data):
        return {"name": name, "mimeType": "application/octet-stream", "buffer": data}

    with serve_database(tmp_path / "runs.sqlite3") as port, sync_playwright() as browser_driver:
        base = f"http://127.0.0.1:{port}"
        with browser_driver.chromium.launch(executable_path=chrome) as browser:
            page = browser.new_page(viewport={"width": 1100, "height": 800})
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.on("console", lambda msg: errors.append(msg.text) if msg.type in ("warning", "error") else None)
            page.goto(base + "/imports")
            field = page.get_by_label("FIT files", exact=True)
            field.set_input_files([selected("first.fit", first), selected("renamed.fit", first)])
            field.focus()
            page.keyboard.press("Tab")
            expect(page.get_by_role("button", name="Upload FIT files")).to_be_focused()
            page.keyboard.press("Enter")
            expect(page.get_by_text("Complete", exact=True)).to_be_visible()
            page.get_by_role("link", name="Activity 1", exact=True).first.click()
            expect(page.get_by_role("cell", name="312.25 m")).to_be_visible()
            page.set_viewport_size({"width": 375, "height": 812})
            page.get_by_role("link", name="Imports", exact=True).click()
            field.set_input_files(selected("similar.fit", make_fit(start=START + 30)))
            page.get_by_role("button", name="Upload FIT files").click()
            expect(page.get_by_role("button", name="Keep separate")).to_be_visible()
            page.get_by_role("link", name="Imports", exact=True).click()
            separate = page.get_by_role("button", name="Keep separate")
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            separate.focus()
            page.keyboard.press("Enter")
            expect(page.get_by_text("Complete", exact=True)).to_be_visible()
            expect(page.get_by_role("link", name="Activity 2", exact=True).first).to_be_visible()
            page.get_by_role("link", name="Imports", exact=True).click()
            field.set_input_files(selected("link.fit", make_fit(start=START + 45)))
            page.get_by_role("button", name="Upload FIT files").click()
            page.get_by_label("Possible activity").select_option("1")
            page.get_by_role("button", name="Link to activity").click()
            expect(page.get_by_role("link", name="Open activity 1")).to_be_visible()
            page.get_by_role("link", name="Imports", exact=True).click()
            field.set_input_files(selected("undated.fit", make_fit(start=START + 900, offset=None)))
            page.get_by_role("button", name="Upload FIT files").click()
            page.get_by_label("Confirmed local date").fill("2024-02-29")
            page.get_by_role("button", name="Save local date").click()
            expect(page.get_by_text("Complete", exact=True)).to_be_visible()
            page.get_by_role("link", name="Open activity 3").click()
            expect(page.get_by_text("2024-02-29", exact=True)).to_be_visible()
            page.get_by_role("link", name="Imports", exact=True).click()
            with page.expect_response(base + "/api/imports/garmin") as normal:
                page.get_by_role("button", name="Sync Garmin").click()
            assert normal.value.status == 202 and normal.value.request.post_data_json == {}
            expect(page.get_by_text("Coverage", exact=True)).to_be_visible()
            assert errors == []


def test_offline_history_month_week_detail_and_json_in_chrome(tmp_path):
    chrome = chrome_executable()
    from playwright.sync_api import expect, sync_playwright

    start = convert_datetime_to_timestamp(datetime(2026, 10, 19, 9, tzinfo=timezone.utc))
    fixture = make_fit(start=start, laps=[(4000, 1200, 1300), (1000, 300, 500)],
                       metrics={"total_distance": 5000, "total_timer_time": 1500, "total_elapsed_time": 1800})
    with serve_database(tmp_path / "runs.sqlite3") as port, sync_playwright() as driver:
        base = f"http://127.0.0.1:{port}"
        with driver.chromium.launch(executable_path=chrome) as browser:
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            errors, requests = [], []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.on("console", lambda msg: errors.append(msg.text) if msg.type in ("warning", "error") else None)
            page.on("requestfailed", lambda req: errors.append(req.url))
            page.on("response", lambda res: errors.append(res.url) if res.status >= 400 else None)
            page.on("request", lambda req: requests.append((req.method, req.url)))
            page.goto(base + "/")
            expect(page.get_by_role("heading", level=1)).to_have_text("History")
            page.get_by_role("link", name="Imports", exact=True).click()
            page.get_by_label("FIT files", exact=True).set_input_files(
                {"name": "synthetic.fit", "mimeType": "application/octet-stream", "buffer": fixture})
            page.get_by_role("button", name="Upload FIT files").click()
            expect(page.get_by_text("Complete", exact=True)).to_be_visible()
            page.get_by_role("link", name="History", exact=True).click()
            boundary = len(requests)
            for unit, width, title in (("month", 1280, "2026-10-01 to 2026-10-31"),
                                       ("week", 375, "2026-10-19 to 2026-10-25")):
                page.set_viewport_size({"width": width, "height": 812})
                page.get_by_label("Period", exact=True).select_option(unit)
                anchor = page.get_by_label("Date in period")
                anchor.fill("2026-10-23")
                page.get_by_label("Period", exact=True).focus()
                page.keyboard.press("Tab")
                expect(anchor).to_be_focused()
                show = page.get_by_role("button", name="Show history")
                for _ in range(6):  # Chrome exposes individual date segments to the keyboard.
                    page.keyboard.press("Tab")
                    if show.evaluate("button => button === document.activeElement"):
                        break
                expect(show).to_be_focused()
                page.keyboard.press("Enter")
                expect(page.get_by_role("heading", name=title)).to_be_visible()
                expect(page.get_by_text("Garmin coverage incomplete.", exact=False)).to_be_visible()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                with page.expect_response(lambda res: "/api/history?" in res.url) as raw:
                    page.get_by_role("link", name="History JSON").click()
                assert raw.value.json()["totals"]["distance_m"]["value"] == 5000
                page.go_back()
                run = page.locator(".run-list").get_by_role("link", name="Activity 1")
                run.focus()
                page.keyboard.press("Enter")
                expect(page.get_by_text("5:00/km", exact=True)).to_be_visible()
                expect(page.get_by_text("25:00", exact=True)).to_be_visible()
                expect(page.get_by_text("30:00", exact=True)).to_be_visible()
                for label in ("Moving time", "Average heart rate", "GPS"):
                    expect(page.get_by_text(label, exact=True).locator("+ dd")).to_have_text("Not available")
                expect(page.get_by_role("row").last).to_contain_text("1000 m")
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                with page.expect_response(base + "/api/activities/1") as detail:
                    page.get_by_role("link", name="Activity JSON").click()
                assert [lap["distance_m"] for lap in detail.value.json()["laps"]] == [4000, 1000]
                page.go_back()
                page.go_back()
            assert all(url.startswith(base + "/") for _, url in requests)
            assert all(method == "GET" for method, _ in requests[boundary:])
            assert errors == []
