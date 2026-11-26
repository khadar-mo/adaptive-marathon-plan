"""Keyboard submission of real saved revisions and occupied time."""
import json

import pytest

from test_calendar_fit import WEEK, client, monday, timed
from test_fit_browser import chrome_executable


@pytest.mark.parametrize("width", [1240, 390])
def test_calendar_diagnostic_keyboard_and_immutable_result(client, width):
    from playwright.sync_api import expect, sync_playwright

    row = monday(client, [("08:00", "09:00")], limit=2)
    fixed = timed(client, "synthetic", 1200)
    base = str(client.base_url).rstrip("/")
    with sync_playwright() as driver, driver.chromium.launch(executable_path=chrome_executable()) as browser:
        page = browser.new_page(viewport={"width": width, "height": 850})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
        page.goto(base + "/availability?week=" + WEEK + "&version=" + row["version_id"])
        page.get_by_role("link", name="Check workout time").press("Enter")
        expect(page.get_by_label("Availability revision")).to_have_value(row["version_id"])
        version = fixed["template_id"] + "/" + fixed["template_version_id"]
        page.get_by_label("Workout revision").select_option(version)
        page.get_by_role("button", name="Add workout").press("Enter")
        second = page.get_by_role("group", name="Workout 2")
        second.get_by_label("Workout revision").select_option(version)
        page.get_by_role("button", name="Remove workout").press("Enter")
        expect(page.get_by_label("Workout revision")).to_have_count(1)
        page.get_by_role("button", name="Add workout").press("Enter")
        second.get_by_label("Workout revision").select_option(version)
        page.get_by_label("Earliest allowed start").fill(WEEK + "T08:01Z")
        busy = [{"id":"appointment", "kind":"NON_RUNNING", "start_utc":WEEK+"T08:30Z", "end_utc":WEEK+"T08:40Z"}]
        page.get_by_label("Occupied intervals").fill(json.dumps(busy))
        page.get_by_role("button", name="Check placement").press("Enter")
        expect(page.get_by_role("heading", name="Workout time result")).to_be_visible()
        expect(page.get_by_text("FEASIBLE", exact=True)).to_be_visible()
        expect(page.get_by_text(WEEK + "T08:40:00+00:00 to " + WEEK + "T09:00:00+00:00", exact=False).first).to_be_visible()
        page.get_by_text("Exact UTC intervals, source revisions and assumptions").press("Enter")
        audit = json.loads(page.locator("pre").inner_text())
        assert audit["availability_version_id"] == row["version_id"]
        assert {s["template_version_id"] for s in audit["placements"]} == {fixed["template_version_id"]}
        assert audit["assumptions"]["occupied"][0]["id"] == "appointment"
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert not errors
