"""Real Chromium coverage of the template editor and exact W2 block accounting."""
from urllib.parse import urljoin

from test_fit_browser import chrome_executable
from test_health import serve_database


def save_from_editor(page):
    from playwright.sync_api import expect

    form = page.locator("#template-editor")
    invalid = form.evaluate("""form => Array.from(form.elements)
        .filter(field => field.willValidate && !field.validity.valid)
        .map(field => [field.name, field.validationMessage])""")
    assert invalid == []
    api_path = form.get_attribute("action")
    api_url = urljoin(page.url, api_path)
    detail_path = api_path.removeprefix("/api")
    with page.expect_response(lambda r: r.url == api_url and r.request.method == "POST") as posted:
        page.get_by_role("button", name="Save workout template").press("Enter")
    response = posted.value
    assert response.status == 200, response.text()
    assert response.headers["content-type"].startswith("application/json")
    expect(page).to_have_url(urljoin(api_url, detail_path))
    return response.request


def test_w2_normal_form_round_trip(tmp_path):
    from playwright.sync_api import expect, sync_playwright

    with serve_database(tmp_path / "runs.sqlite3") as port, sync_playwright() as driver:
        with driver.chromium.launch(executable_path=chrome_executable()) as browser:
            page = browser.new_page(viewport={"width":1240,"height":900})
            base = f"http://127.0.0.1:{port}"
            page.goto(base + "/workouts")
            script_url = base + "/assets/workout-template-editor.js"
            held = []
            page.route(script_url, lambda route: held.append(route), times=1)
            with page.expect_request(script_url):
                page.get_by_role("link", name="Workout templates").press("Enter", no_wait_after=True)
            expect(page.get_by_role("status")).to_have_text("Loading the workout template editor…")
            expect(page.locator("#template-editor")).to_be_attached()
            expect(page.locator("#template-editor")).to_be_hidden()
            held.pop().continue_()
            page.get_by_label("Template name").fill("W2 arithmetic fixture")
            page.get_by_label("Primary purpose").select_option("TEMPO_THRESHOLD")
            page.get_by_label("Intended role").select_option("PRIMARY_QUALITY")
            page.get_by_label("Secondary tags").fill('["BENCHMARK_TAG"]')
            doses = [("WARM_UP", 5000), ("WORK", 6000), ("COOL_DOWN", 16000)]
            for i, (kind, distance) in enumerate(doses):
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
            posted = save_from_editor(page)
            sent = posted.post_data_json
            assert (sent["name"], sent["primary"], sent["role"], sent["tags"]) == (
                "W2 arithmetic fixture", "TEMPO_THRESHOLD", "PRIMARY_QUALITY", ["BENCHMARK_TAG"])
            assert [(b["kind"], b["distance_m"]) for b in sent["blocks"]] == doses
            expect(page.get_by_text("27,000 m total", exact=True)).to_be_visible()
            current = page.request.get(posted.url)
            assert current.status == 200
            record = current.json()["record"]
            api = page.get_by_role("link", name="Exact revision JSON").get_attribute("href")
            assert api == f"/api/workout-templates/{record['template_id']}/versions/{record['version_id']}"
            persisted = page.request.get(base + api)
            assert persisted.status == 200
            r = persisted.json()
            assert r == record
            assert all(r[key] == sent[key] for key in ("name", "primary", "role", "tags"))
            assert [(b["kind"], b["distance_m"]) for b in r["blocks"]] == doses
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
            save_from_editor(page)
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
            save_from_editor(page)
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
