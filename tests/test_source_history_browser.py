"""Real FIT upload, activity navigation and knowledge cutoff inspection in Chromium."""
from datetime import datetime, timedelta

from playwright.sync_api import expect, sync_playwright

from fit_fixture import make_fit
from test_fit_browser import chrome_executable
from test_health import serve_database


def test_activity_source_history_browser_cutoff_and_json(tmp_path):
    with serve_database(tmp_path / "runs.sqlite3") as port, sync_playwright() as driver:
        base = f"http://127.0.0.1:{port}"
        with driver.chromium.launch(executable_path=chrome_executable()) as browser:
            page = browser.new_page(viewport={"width":1280,"height":900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("console", lambda msg: errors.append(msg.text) if msg.type in ("warning","error") else None)
            page.goto(base + "/imports")
            page.get_by_label("FIT files",exact=True).set_input_files(
                {"name":"synthetic.fit","mimeType":"application/octet-stream","buffer":make_fit()})
            page.get_by_role("button",name="Upload FIT files",exact=True).click()
            expect(page.get_by_text("Complete",exact=True)).to_be_visible()
            page.goto(base + "/activities/1")
            page.get_by_role("link",name="Inspect source and identity history").press("Enter")
            expect(page.get_by_role("heading",name="Source and identity history")).to_be_visible()
            page.get_by_text("Checksum and evidence",exact=True).click()
            expect(page.get_by_text("First evidence for this owner",exact=True)).to_be_visible()
            link = page.get_by_role("link",name="Source history JSON")
            response = page.request.get(base + link.get_attribute("href")).json()
            first = response["groups"][0]["identity_evidence"]["known_at"]
            before = (datetime.fromisoformat(first) - timedelta(microseconds=1)).isoformat()
            page.set_viewport_size({"width":375,"height":780})
            page.get_by_label("Knowledge cutoff (ISO time with offset)").fill(before)
            page.get_by_role("button",name="Inspect cutoff").press("Enter")
            expect(page.get_by_text("No timestamped evidence establishes this ownership at the cutoff.")).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.get_by_role("link",name="Source history JSON").press("Enter")
            assert "UNKNOWN" in page.inner_text("body")
            assert not errors
