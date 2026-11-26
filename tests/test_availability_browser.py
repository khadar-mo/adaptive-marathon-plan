"""Real calendar edits, clock errors and version conflicts through the rendered form."""
from datetime import date, timedelta

from test_fit_browser import chrome_executable
from test_health import serve_database
from test_availability import available, submission, window


def test_api_precise_overnight_and_minute_times_survive_browser_note_only_edit(tmp_path):
    from playwright.sync_api import expect, sync_playwright
    chrome = chrome_executable()
    week = "2025-12-29"
    body = available(submission(week), 2, [window("2025-12-31T23:45:00.123456", "2026-01-01T00:15:00.123456")])
    available(body, 1, [window("2025-12-30T10:00", "2025-12-30T11:00")])
    available(body, 0, [window("2025-12-29T12:00:00", "2025-12-29T13:00:00")])
    with serve_database(tmp_path / "runs.sqlite3") as port, sync_playwright() as driver:
        base = f"http://127.0.0.1:{port}"
        with driver.chromium.launch(executable_path=chrome) as browser:
            page = browser.new_page(viewport={"width": 375, "height": 850})
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.on("console", lambda msg: errors.append(msg.text) if msg.type in ("warning", "error") else None)
            page.on("requestfailed", lambda req: errors.append(req.url))
            page.on("response", lambda res: errors.append(res.url) if res.status >= 400 else None)
            response = page.request.post(base + "/api/availability/" + week, data=body)
            assert response.status == 200
            first = response.json()["record"]
            page.goto(base + "/availability?week=" + week)
            precise = page.locator('.availability-day[data-date="2025-12-31"]')
            expect(precise.get_by_label("Start London date and time", exact=True)).to_have_value("2025-12-31T23:45:00.123456")
            expect(precise.get_by_label("End London date and time", exact=True)).to_have_value("2026-01-01T00:15:00.123456")
            minute = page.locator('.availability-day[data-date="2025-12-30"]')
            expect(minute.get_by_label("Start London date and time", exact=True)).to_have_value("2025-12-30T10:00")
            page.get_by_label("Change note (optional)", exact=True).fill("Availability unchanged; note corrected")
            with page.expect_navigation(wait_until="domcontentloaded"), page.expect_response(base + "/api/availability/" + week) as saved:
                page.get_by_role("button", name="Save weekly availability", exact=True).press("Enter")
            assert saved.value.status == 200
            second = page.request.get(base + "/api/availability/" + week).json()["current"]
            assert second["previous_revision_id"] == first["version_id"]
            assert second["submitted"]["days"] == first["submitted"]["days"]
            assert second["days"] == first["days"] and second["union_windows"] == first["union_windows"]
            assert second["days"][2]["windows"][0]["start"]["utc"] == "2025-12-31T23:45:00.123456+00:00"
            assert second["days"][2]["windows"][0]["end"]["utc"] == "2026-01-01T00:15:00.123456+00:00"
            assert page.request.get(base + "/api/availability/" + week + "/versions/" + first["version_id"]).json() == first
            # Ordinary minute entry continues through the same editor alongside precise saved values.
            section = page.locator('.availability-day[data-date="2026-01-01"]')
            section.get_by_role("button", name="Add time window").click()
            section.get_by_label("Start London date and time", exact=True).fill("2026-01-01T08:00")
            section.get_by_label("End London date and time", exact=True).fill("2026-01-01T09:00")
            with page.expect_navigation(wait_until="domcontentloaded"), page.expect_response(base + "/api/availability/" + week) as saved:
                page.get_by_role("button", name="Save weekly availability", exact=True).press("Enter")
            assert saved.value.status == 200
            third = page.request.get(base + "/api/availability/" + week).json()["current"]
            assert third["days"][2] == first["days"][2]
            assert third["days"][3]["windows"][0]["start"]["submitted"]["local"] == "2026-01-01T08:00"
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert not errors


def test_weekly_availability_edit_empty_history_stale_and_dst(tmp_path):
    from playwright.sync_api import expect, sync_playwright
    chrome = chrome_executable()
    with serve_database(tmp_path / "runs.sqlite3") as port, sync_playwright() as driver:
        base = f"http://127.0.0.1:{port}"
        with driver.chromium.launch(executable_path=chrome) as browser:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.set_default_timeout(10000)
            errors, failed, responses, console = [], [], [], []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.on("requestfailed", lambda req: failed.append(req.url))
            page.on("response", lambda response: responses.append((response.url, response.status)) if response.status >= 400 else None)
            page.on("console", lambda msg: console.append(msg.text) if msg.type in ("error", "warning") else None)

            def choose_week(week):
                page.get_by_label("Week beginning Monday", exact=True).fill(week)
                with page.expect_navigation(wait_until="domcontentloaded"):
                    page.get_by_role("button", name="Open week", exact=True).press("Enter")

            def add(day, start, end):
                section = page.locator(f'.availability-day[data-date="{day}"]')
                section.get_by_role("button", name="Add time window").click()
                row = section.locator(".time-window").last
                row.get_by_label("Start London date and time", exact=True).fill(start)
                row.get_by_label("End London date and time", exact=True).fill(end)
                return row

            def save(week):
                with page.expect_navigation(wait_until="domcontentloaded"), page.expect_response(base + "/api/availability/" + week) as result:
                    page.get_by_role("button", name="Save weekly availability", exact=True).press("Enter")
                assert result.value.status == 200
                # The form deliberately navigates after consuming the response;
                # inspect the persisted result rather than a discarded network body.
                return page.request.get(base + "/api/availability/" + week).json()["current"]

            page.goto(base + "/context")
            page.get_by_role("link", name="Weekly availability", exact=True).press("Enter")
            choose_week("2026-07-06")
            expect(page.locator(".availability-status")).to_contain_text("UNKNOWN — not entered")
            page.get_by_label("Submitted by", exact=True).fill("Synthetic browser runner")
            for i in range(7):
                day = (date(2026, 7, 6) + timedelta(days=i)).isoformat()
                page.get_by_role("combobox", name="Availability for " + day, exact=True).select_option("UNAVAILABLE")
                if i < 5:
                    add(day, day + "T10:00", day + "T11:00")
            add("2026-07-06", "2026-07-06T10:30", "2026-07-06T12:00")
            page.get_by_label("Maximum sessions on 2026-07-06", exact=True).fill("2")
            page.get_by_label("I permit doubles or more than one session on 2026-07-06", exact=True).check()
            first = save("2026-07-06")
            assert first["union_elapsed_seconds"] == 21600 and len(first["union_windows"]) == 5
            stale = browser.new_page()
            stale.goto(base + "/availability?week=2026-07-06")
            for day in ("2026-07-09", "2026-07-10"):
                page.get_by_role("combobox", name="Availability for " + day, exact=True).select_option("UNAVAILABLE")
            second = save("2026-07-06")
            assert second["union_elapsed_seconds"] == 14400 and len(second["union_windows"]) == 3
            assert second["previous_revision_id"] == first["version_id"]
            page.get_by_role("link", name="Revision 1", exact=True).click()
            expect(page.get_by_role("heading", name="Saved revision 1", exact=True)).to_be_visible()
            assert page.locator("#availability-editor").count() == 0
            expect(page.get_by_text("21600 seconds", exact=True)).to_be_visible()
            page.get_by_role("link", name="Open current availability to edit", exact=True).click()
            for i in range(7):
                day = (date(2026, 7, 6) + timedelta(days=i)).isoformat()
                page.get_by_role("combobox", name="Availability for " + day, exact=True).select_option("UNAVAILABLE")
            third = save("2026-07-06")
            assert third["state"] == "EMPTY"
            expect(page.get_by_text("This is an explicitly empty week, not a missing submission.", exact=True)).to_be_visible()
            with stale.expect_response(base + "/api/availability/2026-07-06") as result:
                stale.get_by_role("button", name="Save weekly availability").click()
            assert result.value.status == 409
            expect(stale.get_by_role("status")).to_contain_text("changed since this form")
            assert page.request.get(base + "/api/availability/2026-07-06").json()["current"] == third
            stale.close()
            assert not console and not responses and not errors and not failed

            choose_week("2026-03-23")
            page.get_by_label("Submitted by", exact=True).fill("Synthetic clock check")
            row = add("2026-03-29", "2026-03-29T01:30", "2026-03-29T03:00")
            with page.expect_response(base + "/api/availability/2026-03-23") as result:
                page.get_by_role("button", name="Save weekly availability").press("Enter")
            assert result.value.status == 422
            expect(page.get_by_role("status")).to_contain_text("does not exist")
            row.get_by_label("Start London date and time", exact=True).fill("2026-03-29T00:30")
            row.get_by_label("End London date and time", exact=True).fill("2026-03-29T02:30")
            assert save("2026-03-23")["union_elapsed_seconds"] == 3600

            choose_week("2026-10-19")
            page.get_by_label("Submitted by", exact=True).fill("Synthetic clock check")
            row = add("2026-10-25", "2026-10-25T01:15", "2026-10-25T01:45")
            with page.expect_response(base + "/api/availability/2026-10-19") as result:
                page.get_by_role("button", name="Save weekly availability").click()
            assert result.value.status == 422
            expect(page.get_by_role("status")).to_contain_text("occurs twice")
            row.get_by_text("Clock-change choices and offsets", exact=True).press("Enter")
            row.get_by_role("combobox", name="Start clock occurrence", exact=True).select_option("0")
            row.get_by_role("combobox", name="End clock occurrence", exact=True).select_option("1")
            assert save("2026-10-19")["union_elapsed_seconds"] == 5400
            for width in (1280, 375):
                page.set_viewport_size({"width": width, "height": 850})
                page.get_by_text("Original local choices, UTC bounds and daily limits", exact=True).press("Enter")
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert not errors and not failed
            assert [status for _, status in responses] == [422, 422]
            assert all("422" in message and "Failed to load resource" in message for message in console)
