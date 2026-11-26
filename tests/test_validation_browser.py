"""Exercise the runner's capture, scheduling and replay controls on desktop and mobile."""
from datetime import datetime, timedelta, timezone

from playwright.sync_api import expect, sync_playwright
from test_fit_browser import chrome_executable
from test_fit_summary_spans import summary_fit
from test_health import serve_database


def test_browser_capture_and_schedule(tmp_path):
    with serve_database(tmp_path / "runs.sqlite3") as port, sync_playwright() as driver:
        base = f"http://127.0.0.1:{port}"
        with driver.chromium.launch(executable_path=chrome_executable()) as browser:
            page = browser.new_page(viewport={"width":1280,"height":900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base + "/imports")
            page.get_by_label("FIT files", exact=True).set_input_files({
                "name":"benchmark.fit", "mimeType":"application/octet-stream", "buffer":summary_fit()})
            page.get_by_role("button", name="Upload FIT files").click()
            expect(page.get_by_text("Complete", exact=True)).to_be_visible()
            page.get_by_role("link", name="Fitness profile", exact=True).click()
            page.get_by_role("link", name="Fitness snapshots and benchmarks", exact=True).click()
            page.get_by_role("button", name="Save snapshot", exact=True).click()
            expect(page.get_by_role("cell", name="1500.375", exact=True)).to_be_visible()
            source = page.get_by_role("link", name="Frozen source and ownership")
            frozen = page.request.get(base + source.get_attribute("href")).json()
            assert frozen["activity"]["source_parser"]["normalization_version"]
            assert frozen["source_evidence"]["sources"][0]["first_owner_evidence"]
            scheduled = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
            page.get_by_label("Benchmark name").fill("Track half-mile")
            page.get_by_label("Scheduled start (UTC or offset)").fill(scheduled)
            page.get_by_label("Target distance (metres)").fill("804.672")
            page.get_by_role("button", name="Freeze forecast").click()
            expect(page.get_by_role("heading", name="Track half-mile")).to_be_visible()
            expect(page.get_by_role("cell", name="NO_QUALIFIED_SAME_DISTANCE_WHOLE_WORKOUT")).to_be_visible()
            expect(page.get_by_text("Positive bias means", exact=False)).to_be_visible()
            replay = page.get_by_role("link", name="Replay frozen forecast").get_attribute("href")
            assert page.request.get(base + replay).json()["status"] == "IDENTICAL"
            page.set_viewport_size({"width":390,"height":844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert not errors
