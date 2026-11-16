import os
import shutil

import pytest

from fit_fixture import START, make_fit
from test_health import serve_database


def test_upload_and_resolution_in_chrome(tmp_path):
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    if not chrome and not os.environ.get("GITHUB_ACTIONS"):
        pytest.skip("Install Chrome or Chromium on PATH to run the browser replay")
    assert chrome, "The browser replay requires Chrome on the runner"
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
            assert errors == []
