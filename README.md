# Adaptive marathon plan

## Check Garmin or Strava

Run either command from this directory with Python 3. The provider commands use the standard library and work with the local app stopped. Set the selected provider's credentials in `.env` (see `.env.example`) or your environment.

```sh
python3 runs.py garmin --limit 1
python3 runs.py strava --limit 1
```

The default is ten runs. Use `--limit` to request 1–10. An empty list is a successful connection. To grant Strava activity access or reconnect, run `python3 runs.py strava --connect`. The example callback uses port 8000.

For Garmin, **Time** is the watch timer duration (`duration`). Calculate training pace only when timer time and distance are both positive, by dividing timer seconds by kilometres. **Elapsed Time** (`elapsedDuration`) covers the whole activity from start to save, including pauses. If a timing field is missing, its column shows a dash. [Garmin’s timing definitions](https://support.garmin.com/en-GB/?faq=k5TPjwyAWi5f4hnObUAVf7)

If Garmin rejects the token exchange with HTTP 401, the message identifies where the CLI found the key. An exported `GARMIN_PERSONAL_API_KEY` takes precedence over the value in `.env`. To try the file's value for one run, use `env -u GARMIN_PERSONAL_API_KEY python3 runs.py garmin --limit 1`. Use the raw key with no authorization prefix. You can check the key's active status and expiration on the Personal API Key page in Garmin Connect. [Garmin authentication FAQ](https://developer.garmin.com/gc-developer-program/personal/personal-faq)

To verify Garmin, stop the local app, run the one-run command above and open the activity link in Garmin Connect. Compare the ID in the link, recorded local start, distance in kilometres at the same precision, and **Elapsed Time** within one second under **Stats → Timing**. The **Time** row there corresponds to the CLI's first duration column. Keep the activity and measurements private. Report only the match result.

## Import a Garmin run

From the repository directory, use Python 3.11 or newer to install the server and start one local process.

```sh
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -m uvicorn app:app --host 127.0.0.1 --port 8001 --no-access-log
```

Open [Imports](http://127.0.0.1:8001/imports), enter the activity's recorded local date in both fields and select **Sync Garmin**. The saved report links to the local activity, its JSON and any download warning. **Recheck these dates** retries the range. A retry keeps the same activity ID. This first importer checks one page of the selected range and flags a full page for follow-up.

The first valid submission creates `runs.sqlite3` in the working directory and archives Garmin's original response bytes in `runs-archive/` beside the database. To keep them elsewhere, set `RUNS_DB_PATH` and optionally `RUNS_ARCHIVE_PATH` before starting the server. Both locations hold private activity data. Stop the server and back up or restore the pair together.

To reopen a run offline, stop the process and repeat the start command with `RUNS_GARMIN_OFFLINE=1` and the same storage paths. Open the saved report and the activity's **Activity JSON** link. Startup, report refresh and activity views use local storage. An interrupted report retains its dates and any activities already committed.

The JSON endpoint accepts an inclusive pair of ISO dates at `POST /api/imports/garmin`, returning HTTP 202 with `id`, `report_url` and `html_url`. `GET /api/imports` lists attempts newest first. `GET /api/imports/{id}` and `GET /api/activities/{id}` read individual reports and activities. Date validation runs before storage writes. A concurrent submission returns HTTP 409 and the running report links.

## Check local storage

In another terminal, run `python3 runs.py health`. It calls only `GET http://127.0.0.1:8001/health`. The server opens SQLite read-only and reads its schema. A readable database, including an empty one, returns HTTP 200 and `{"status":"ok"}`. A missing, unreadable or invalid database returns 503. The CLI exits nonzero on failure. To check health before the first import, create an empty database with `python3 -c "from pathlib import Path; Path('runs.sqlite3').touch(mode=0o600)"` in the working directory.

To run the tests, activate the virtual environment, install `requirements-dev.txt` with pip and run `python3 -m pytest -q`. The tests use synthetic activities, temporary databases and loopback HTTP.
