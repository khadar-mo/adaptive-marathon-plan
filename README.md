# Adaptive marathon plan

Check the connection to your own Garmin or Strava account by listing recent runs in the terminal. Run from this directory with Python 3. The script uses the standard library and reads credentials from the existing `.env`. A fresh checkout can use `.env.example` as its template.

```sh
python3 runs.py garmin
python3 runs.py strava
```

Each command prints up to ten runs. For a quick check, add `--limit 1`. Open the link at the end of the row and compare the local start time, distance in kilometres and **elapsed time** with the activity on the platform.
