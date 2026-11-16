# Adaptive marathon plan

## Check Garmin or Strava

Run either command from this directory with Python 3. The provider commands use the standard library and work with the local app stopped. Set the selected provider's credentials in `.env` (see `.env.example`) or your environment.

```sh
python3 runs.py garmin --limit 1
python3 runs.py strava --limit 1
```

The default is ten runs. Use `--limit` to request 1–10. An empty list is a successful connection. To grant Strava activity access or reconnect, run `python3 runs.py strava --connect`. The example callback uses port 8000.

To verify Garmin, stop the local app, run the one-run command above and open the activity link in Garmin Connect. Compare the ID in the link, recorded local start, distance in kilometres at the same precision, and **elapsed time** within one second. Keep the activity and measurements private. Report only whether all four fields matched.

## Check local storage

From the repository directory, install the server and create an empty local database on first use.

```sh
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -c "import sqlite3; sqlite3.connect('runs.sqlite3').close()"
python3 -m uvicorn app:app --host 127.0.0.1 --port 8001
```

To use an existing SQLite file, set `RUNS_DB_PATH` in the server's environment and skip the database creation step. The default is `runs.sqlite3` in the server's working directory.

In another terminal, run `python3 runs.py health`. It calls only `GET http://127.0.0.1:8001/health`. The server opens SQLite read-only and reads its schema. A readable database, including an empty one, returns HTTP 200 and `{"status":"ok"}`. A missing, unreadable or invalid database returns 503. The CLI exits nonzero on failure.

To run the tests, activate the virtual environment, install `requirements-dev.txt` with pip and run `python3 -m pytest -q`. The tests use synthetic activities, temporary databases and loopback HTTP.
