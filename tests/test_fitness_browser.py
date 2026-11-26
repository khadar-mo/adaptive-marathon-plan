"""Real app navigation, editable descriptors and snapshot replay at two widths."""
from test_fit_browser import chrome_executable
from test_health import serve_database
from test_fitness import steady


def test_chromium_fitness_inputs_versions_and_frozen_changes(tmp_path):
    from playwright.sync_api import expect, sync_playwright
    chrome = chrome_executable()
    with serve_database(tmp_path / "runs.sqlite3") as port, sync_playwright() as driver:
        base = f"http://127.0.0.1:{port}"
        with driver.chromium.launch(executable_path=chrome) as browser:
            page = browser.new_page(viewport={"width":1280,"height":900})
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.on("console", lambda msg: errors.append(msg.text) if msg.type in ("error", "warning") else None)
            page.on("requestfailed", lambda req: errors.append(req.url))
            page.goto(base + "/imports")
            page.get_by_label("FIT files", exact=True).set_input_files({"name":"park.fit","mimeType":"application/octet-stream","buffer":steady(0)})
            page.get_by_role("button", name="Upload FIT files").press("Enter")
            expect(page.get_by_text("Complete", exact=True)).to_be_visible()
            page.get_by_role("link", name="Fitness profile", exact=True).press("Enter")
            expect(page.get_by_role("heading", level=1)).to_have_text("Fitness profile")
            page.get_by_role("link", name="Edit comparison evidence for run 1").press("Enter")
            form = page.locator("#fitness-observation")
            for k,v in (("Recorded route","Park loop"),("Recorded conditions","Dry"),("Recorded grade (%)","0"),
                        ("Observation source","Runner"),("Source reference","Synthetic park log"),("Reported by","Athlete"),
                        ("Entry or correction reason","Initial observation")):
                form.get_by_label(k,exact=True).fill(v)
            with page.expect_response(base + "/api/fitness/run-context") as response:
                form.get_by_role("button",name="Save run evidence").press("Enter")
            assert response.value.status == 200
            expect(page.get_by_label("Recorded route",exact=True)).to_have_value("Park loop")
            page.set_viewport_size({"width":375,"height":850})
            page.get_by_text("Define a descriptor version",exact=True).press("Enter")
            form = page.locator("#fitness-config")
            for key,value in (("New descriptor version","browser-run"),("Configuration author","Runner"),("Reason for this definition","Park comparison"),
                              ("Matched route","Park loop"),("Matched conditions","Dry"),("Duration (seconds) minimum","290"),
                              ("Duration (seconds) maximum","310"),("Grade (%) minimum","-0.5"),("Grade (%) maximum","0.5"),
                              ("Heart rate (bpm) minimum","140"),("Heart rate (bpm) maximum","150"),
                              ("Sustained pace (seconds/km) minimum","290"),("Sustained pace (seconds/km) maximum","310"),("Late block after recorded metres","500")):
                form.get_by_label(key,exact=True).fill(value)
            with page.expect_response(base + "/api/fitness/configurations") as response:
                form.get_by_role("button",name="Save descriptor version").press("Enter")
            assert response.value.status == 200
            expect(page.get_by_text("ANECDOTAL · 1 matched · Median pace 300 s/km.",exact=True)).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            with page.expect_response(base + "/api/fitness/capture") as saved:
                page.get_by_role("button",name="Save fitness snapshot").press("Enter")
            assert saved.value.status == 200
            expect(page.get_by_role("heading",name="Changes in this snapshot")).to_be_visible()
            identity = page.url.rsplit("/", 1)[1]
            page.get_by_text("Changed fields, reasons and values",exact=True).press("Enter")
            assert "endurance.comparable_effort" in page.locator("pre").first.inner_text()
            with page.expect_response(base + "/api/fitness/snapshots/"+identity+"/replay") as replay:
                page.get_by_role("link",name="Rebuild and compare").press("Enter")
            assert replay.value.json()["status"]=="IDENTICAL"
            assert not errors
