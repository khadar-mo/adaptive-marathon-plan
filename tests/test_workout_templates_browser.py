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
