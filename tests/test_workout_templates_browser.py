"""Real Chromium coverage of the template editor and exact W2 block accounting."""
from test_fit_browser import chrome_executable
from test_health import serve_database


def test_w2_normal_form_round_trip(tmp_path):
    from playwright.sync_api import expect, sync_playwright

    with serve_database(tmp_path / "runs.sqlite3") as port, sync_playwright() as driver:
        with driver.chromium.launch(executable_path=chrome_executable()) as browser:
            page = browser.new_page(viewport={"width":1240,"height":900})
            base = f"http://127.0.0.1:{port}"
            page.goto(base + "/workouts")
            page.get_by_role("link", name="Workout templates").press("Enter")
            page.get_by_label("Template name").fill("W2 arithmetic fixture")
            page.get_by_label("Primary purpose").select_option("TEMPO_THRESHOLD")
            page.get_by_label("Intended role").select_option("PRIMARY_QUALITY")
            page.get_by_label("Secondary tags").fill('["BENCHMARK_TAG"]')
            for i, (kind, distance) in enumerate([("WARM_UP",5000), ("WORK",6000), ("COOL_DOWN",16000)]):
                if i:
                    page.get_by_role("button", name="Add block").press("Enter")
                block = page.get_by_role("group", name=f"Block {i+1}", exact=True)
                block.get_by_label("Block type").select_option(kind)
                block.get_by_label("Distance (metres)").fill(str(distance))
                block.get_by_label("Intended intensity").fill("Tempo" if kind == "WORK" else "Easy")
                block.get_by_label("Intensity method").fill("Synthetic runner description")
                if kind == "WORK":
                    block.get_by_label("Protect this block").check()
            page.get_by_label("Recorded by").fill("Synthetic runner")
            page.get_by_label("Reason for this revision").fill("Check independently specified W2 quantities")
            with page.expect_navigation():
                page.get_by_role("button", name="Save workout template").press("Enter")
            expect(page.get_by_text("27,000 m total", exact=True)).to_be_visible()
            api = page.get_by_role("link", name="Exact revision JSON").get_attribute("href")
            r = page.request.get(base + api).json()
            assert r["primary"] == "TEMPO_THRESHOLD" and r["tags"] == ["BENCHMARK_TAG"]
            assert (r["accounting"]["total_m"], r["accounting"]["work_m"], r["accounting"]["easy_m"]) == (27000,6000,21000)
            assert r["accounting"]["duration_upper_s"] is None
            assert r["blocks"][1]["protected"]

def test_pace_precision_mobile_history_stale_and_invalid_forms(tmp_path):
    from decimal import Decimal, localcontext
    from urllib.parse import urlencode
    from playwright.sync_api import expect, sync_playwright

    precise = "5.1234567890123456789012345678901234567891"
    with serve_database(tmp_path / "runs.sqlite3") as port, sync_playwright() as driver:
        base = f"http://127.0.0.1:{port}"
        with driver.chromium.launch(executable_path=chrome_executable()) as browser:
            page = browser.new_page(viewport={"width": 390, "height": 850})
            failures, errors, bad_responses = [], [], []
            page.on("requestfailed", lambda req: failures.append(req.url))
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("console", lambda line: errors.append(line.text) if line.type == "error" else None)
            page.on("response", lambda resp: bad_responses.append(resp.status) if resp.status >= 400 else None)
            page.goto(base + "/workout-templates")
            page.get_by_label("Template name").fill("Synthetic precise bounds")
            block = page.get_by_role("group", name="Block 1", exact=True)
            block.get_by_label("Distance (metres)").fill("1001")
            block.get_by_label("Intended intensity").fill("Easy effort")
            block.get_by_label("Intensity method").fill("Runner description")
            block.get_by_text("Pace evidence", exact=True).press("Enter")
            block.get_by_label("Include supplied pace bounds").check()
            block.get_by_label("Fastest pace").fill(precise)
            block.get_by_label("Slowest pace").fill("5.35")
            block.get_by_label("Pace unit").select_option("min/km")
            block.get_by_label("Pace method").fill("Fixture laps")
            block.get_by_label("Pace source reference").fill("Synthetic, lap 2")
            block.get_by_label("Pace calibration reference").fill("calibration version 1")
            block.get_by_label("Evidence known at").fill("2025-01-01T12:01:02.123456+01:00")
            block.get_by_label("Pace uncertainty").fill("Illustrative range only")
            block.get_by_label("Evidence status").select_option("SUPPORTED")
            page.get_by_label("Recorded by").fill("Fixture runner")
            page.get_by_label("Reason for this revision").fill("Enter decimal bounds")
            with page.expect_navigation():
                page.get_by_role("button", name="Save workout template").press("Enter")
            url = page.url
            first_url = page.get_by_role("link", name="Exact revision JSON").get_attribute("href")
            first = page.request.get(base + first_url).json()
            assert first["blocks"][0]["pace"]["lower"] == precise
            with localcontext() as ctx:
                ctx.prec = 100
                assert Decimal(first["accounting"]["duration_lower_s"]) == Decimal(precise) * 1001 * Decimal(".06")
            page.get_by_label("Template name").fill("Edited description")
            page.get_by_label("Recorded by").fill("Fixture runner")
            page.get_by_label("Reason for this revision").fill("Rename only")
            with page.expect_navigation():
                page.get_by_role("button", name="Save workout template").press("Enter")
            second_url = page.get_by_role("link", name="Exact revision JSON").get_attribute("href")
            second = page.request.get(base + second_url).json()
            assert second["previous_version_id"] == first["version_id"]
            assert second["blocks"] == first["blocks"] and second["accounting"] == first["accounting"]
            assert page.request.get(base + first_url).json() == first
            page.goto(url + "?" + urlencode({"as_of": first["known_at"]}))
            expect(page.get_by_role("heading", name="Synthetic precise bounds")).to_be_visible()
            assert page.locator("form").count() == 0
            with page.expect_navigation():
                page.get_by_role("link", name="Exact revision JSON").press("Enter")
            assert "as_of=" in page.url and page.request.get(page.url).json() == first

            page.goto(url)
            block = page.get_by_role("group", name="Block 1", exact=True)
            block.get_by_text("Pace evidence", exact=True).press("Enter")
            block.get_by_label("Slowest pace").fill("4")
            page.get_by_label("Recorded by").fill("Test")
            page.get_by_label("Reason for this revision").fill("Invalid bounds fixture")
            with page.expect_response(lambda r: r.request.method == "POST") as invalid:
                page.get_by_role("button", name="Save workout template").press("Enter")
            assert invalid.value.status == 422
            expect(page.get_by_role("status")).to_contain_text("fastest to slowest")
            block.get_by_label("Slowest pace").fill("5.35")
            api = "/api/workout-templates/" + first["template_id"]
            doc = {k: second[k] for k in ("name","primary","role","tags","blocks","actor","reason")}
            doc.update(expected_revision=second["version_id"], request_id="independent-edit")
            third = page.request.post(base + api, data=doc).json()["record"]
            with page.expect_response(lambda r: r.request.method == "POST") as stale:
                page.get_by_role("button", name="Save workout template").press("Enter")
            assert stale.value.status == 409
            expect(page.get_by_role("status")).to_contain_text("changed")
            assert page.request.get(base + api).json()["record"] == third
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert not failures
            assert bad_responses == [422, 409]
            assert all("422" in msg or "409" in msg for msg in errors), errors
