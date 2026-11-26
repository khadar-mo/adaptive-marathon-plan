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


def test_browser_preserves_unquoted_decimal_rule_and_range_when_versioning(tmp_path):
    import json
    from playwright.sync_api import expect, sync_playwright
    digits = "0.12345678901234567890123456789"
    range_end = "350.12345678901234567890123456789"
    raw = ('[{"stream":{"metric":"resting_hr","unit":"bpm","source":"watch","method":"rest","calibration":null},'
           '"epsilon":' + digits + ',"direction":"HIGHER"}]')
    with serve_database(tmp_path / "runs.sqlite3") as port, sync_playwright() as driver:
        base = f"http://127.0.0.1:{port}"
        with driver.chromium.launch(executable_path=chrome_executable()) as browser:
            page = browser.new_page()
            page.goto(base + "/fitness")
            for version in ("exact-a", "exact-b"):
                page.get_by_text("Define a descriptor version",exact=True).click()
                form = page.locator("#fitness-config")
                form.get_by_label("New descriptor version",exact=True).fill(version)
                form.get_by_label("Configuration author",exact=True).fill("runner")
                form.get_by_label("Reason for this definition",exact=True).fill("Precision check")
                if version == "exact-a":
                    form.get_by_label("Robust-deviation rules (JSON array)",exact=True).fill(raw)
                    form.get_by_label("Sustained pace (seconds/km) minimum",exact=True).fill("290")
                    form.get_by_label("Sustained pace (seconds/km) maximum",exact=True).fill(range_end)
                else:
                    assert json.loads(form.get_by_label("Robust-deviation rules (JSON array)").input_value())[0]["epsilon"] == digits
                    expect(form.get_by_label("Sustained pace (seconds/km) maximum",exact=True)).to_have_value(range_end)
                with page.expect_response(base + "/api/fitness/configurations") as response:
                    form.get_by_role("button",name="Save descriptor version").press("Enter")
                assert response.value.status == 200
                page.wait_for_url("**/fitness?**config_version=" + version + "**")
                state = page.request.get(base + "/api/fitness", params={"config_version":version}).json()
                config = state["definition"]["config"]
                assert config["robust_deviations"][0]["epsilon"] == digits
                assert config["sustained_pace_s_per_km"]["high"] == range_end
