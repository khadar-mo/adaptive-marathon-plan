"""Real forms, explicit runner decisions, stale edits and immutable prior versions."""
from urllib.parse import quote

from test_fit_browser import chrome_executable
from test_health import serve_database
from test_runner_settings import API, prefs


def test_runner_settings_keyboard_confirmation_exact_text_history_and_conflicts(tmp_path):
    from playwright.sync_api import expect, sync_playwright
    chrome = chrome_executable()
    with serve_database(tmp_path / "runs.sqlite3") as port, sync_playwright() as driver:
        base = f"http://127.0.0.1:{port}"
        with driver.chromium.launch(executable_path=chrome) as browser:
            page = browser.new_page(viewport={"width": 1240, "height": 900})
            page.set_default_timeout(8000)
            errors, failed, http_errors, console = [], [], [], []
            page.on("pageerror", lambda err: errors.append(str(err)))
            page.on("requestfailed", lambda req: failed.append(req.url))
            page.on("response", lambda res: http_errors.append(res.status) if res.status >= 400 else None)
            page.on("console", lambda msg: console.append(msg.text) if msg.type in ("warning", "error") else None)
            page.goto(base + "/history")
            page.get_by_role("link", name="Runner settings", exact=True).press("Enter")
            goal = page.get_by_role("group", name="Revise the race goal")
            expect(goal.get_by_label("New finish time", exact=False)).to_have_value("2:30:00")
            goal.get_by_label("New finish time", exact=False).fill("2:32:03")
            goal.get_by_label("Decided by").fill("Fixture runner")
            goal.get_by_label("Reason for this decision").fill("My goal review")
            goal.get_by_role("button", name="Record goal decision").press("Enter")
            expect(goal.get_by_role("checkbox")).to_be_focused()
            assert page.request.get(base + API + "/goal").json()["current"]["revision"] == 0
            goal.get_by_role("checkbox").press("Space")
            with page.expect_navigation(wait_until="domcontentloaded"):
                goal.get_by_role("button", name="Record goal decision").press("Enter")
            assert page.request.get(base + API + "/goal").json()["current"]["target_duration_s"] == 9123
            page.get_by_text("Marathon goal history", exact=True).click()
            page.get_by_role("link", name="Original goal").press("Enter")
            expect(page.get_by_text("2:30:00", exact=True)).to_be_visible()
            assert page.locator("form").count() == 0

            # HTML normalizes line endings in a textarea. Editing another field must retain original API text.
            initial = page.request.get(base + API + "/preferences").json()["current"]
            note = "\r\n  Original Ω\t <constraint>   \r\n"
            raw = prefs(initial["version_id"], strength_exercise_choices="  Rows\r\nSplits  ",
                        special_constraints=[{"scope": " Endurance ", "text": note, "through_date": "2027-02-10"}])
            first = page.request.post(base + API + "/preferences", data=raw).json()["record"]
            page.set_viewport_size({"width": 375, "height": 760})
            page.goto(base + "/runner-settings")
            form = page.get_by_role("group", name="Update training preferences")
            expect(form.get_by_label("Constraint in your words")).to_have_value(note.replace("\r\n", "\n"))
            form.get_by_label("Maximum running sessions", exact=False).fill("1")
            form.get_by_label("Decided by").fill("Fixture runner")
            form.get_by_label("Reason for this decision").fill("Keep my running cap at one")
            with page.expect_navigation(wait_until="domcontentloaded"):
                form.get_by_role("button", name="Save training preferences").press("Enter")
            second = page.request.get(base + API + "/preferences").json()["current"]
            assert second["max_sessions_per_day"] == 1 and second["special_constraints"] == first["special_constraints"]
            assert second["strength_exercise_choices"] == raw["strength_exercise_choices"]
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

            form.get_by_label("Special constraints", exact=True).select_option("none")
            assert form.get_by_label("Constraint in your words").count() == 1
            expect(form.get_by_label("Constraint in your words")).not_to_be_visible()
            form.get_by_label("Decided by").fill("Fixture runner")
            form.get_by_label("Reason for this decision").fill("Review after the existing revision")
            later = page.request.post(base + API + "/preferences", data=prefs(second["version_id"], "latest")).json()["record"]
            with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/preferences")) as stale:
                form.get_by_role("button", name="Save training preferences").press("Enter")
            assert stale.value.status == 409
            expect(form.get_by_role("status")).to_contain_text("changed")
            assert page.request.get(base + API + "/preferences").json()["current"] == later

            page.goto(base + "/runner-settings?as_of=" + quote(first["decided_at"], safe=""))
            assert page.locator("form").count() == 0
            with page.expect_navigation(wait_until="domcontentloaded"):
                page.get_by_role("link", name="Training preferences JSON", exact=True).press("Enter")
            assert page.request.get(page.url).json()["current"] == first
            page.goto(base + "/runner-settings")
            form.get_by_label("Special constraints", exact=True).select_option("listed")
            expect(form.get_by_label("Applies to")).to_be_focused()
            form.get_by_label("Applies to").fill("My long run")
            form.get_by_label("Constraint in your words").fill(" Leave Friday free ")
            form.get_by_label("From local date").fill("2027-02-08")
            form.get_by_label("Through local date").fill("2027-02-07")
            form.get_by_label("Decided by").fill("Runner")
            form.get_by_label("Reason for this decision").fill("Work situation")
            with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/preferences")) as rejected:
                form.get_by_role("button", name="Save training preferences").press("Enter")
            assert rejected.value.status == 422
            expect(form.get_by_role("status")).to_contain_text("last local date")
            form.get_by_label("Through local date").fill("2027-02-09")
            with page.expect_navigation(wait_until="domcontentloaded"):
                form.get_by_role("button", name="Save training preferences").press("Enter")
            final = page.request.get(base + API + "/preferences").json()["current"]
            assert final["special_constraints"] == [{"scope": "My long run", "text": " Leave Friday free ",
                                                     "from_date": "2027-02-08", "through_date": "2027-02-09"}]
            assert http_errors == [409, 422] and not errors and not failed
            assert all("409" in msg or "422" in msg for msg in console)
