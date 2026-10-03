# Run the Weather API on Oracle

Read [the implementation plan](marvin-oracle-plan.md) for ownership and compatibility decisions.

## Prepare the Host

Use Ubuntu 24.04 and enough memory for one Chromium process.
An Ampere A1 instance with 1 OCPU and 6 GB RAM fits this service.
Check the Oracle Console's Always Free label, home-region allocation, and estimated price before creating resources.
Oracle may reclaim idle Always Free instances; restart policy covers process/host restarts while the instance exists.

Install Docker Engine and the Compose plugin from the [official Ubuntu installation instructions](https://docs.docker.com/engine/install/ubuntu/).
Allow SSH from the administrator's address and TCP 80/443 for Caddy in both OCI network rules and the host firewall.
Keep port 8080 internal to the Compose network.
Give the host a DNS name that resolves to its public IP.

## Supply Runtime Configuration

Check out this repository into `/opt/stargazing-weather-forecast`.
Copy `.env.example` to `.env`, restrict access with `chmod 600 .env`, and fill in the existing Slack bot token, automatic-report Slack channel, weather API token, and HTTPS hostname.
The application fails to start without `WEATHER_API_TOKEN`.
Share that token with Marvin only through the `space42-weather-api-token` GCP Secret Manager resource, mapped to `SPACE42_WEATHER_API_TOKEN`.
Set Marvin's `SPACE42_WEATHER_BASE_URL` to the Caddy HTTPS URL.
Never commit `.env`, private keys, database files, or report output.

The existing Sheet and tab remain the defaults.
`WEATHER_SPREADSHEET_ID` and `WEATHER_SHEET_GID` allow an explicit replacement.
`WEATHER_SHEET_ENABLED=true` schedules fresh Sheet reports daily at `WEATHER_SHEET_HOUR=9`, in `WEATHER_TIMEZONE=Asia/Yerevan`.
Marvin monitors deliver to their originating Slack channel and do not modify the Sheet.

## Start and Verify

```bash
cd /opt/stargazing-weather-forecast
docker compose --env-file .env -f deploy/compose.yml up -d --build
docker compose --env-file .env -f deploy/compose.yml ps
curl --fail https://your-weather-hostname/healthz
```

The image uses Python 3.12, installs the matching Playwright Chromium binary and Linux dependencies, and runs as an unprivileged user.
Gunicorn uses exactly one worker and four HTTP threads.
The scheduler obtains an exclusive database-adjacent lock; multiple app workers must not schedule the same database.
Compose restarts containers after host restarts and stores monitor state in `weather-data`.

Use authenticated API requests to list the protected Sheet monitor and a Marvin monitor.
Verify a created monitor independently through `GET /v1/monitors/{id}`.
After stopping a monitor, check its `status` and the stop receipt's `cancellation_confirmed` value.
A `202` stop receipt means cancellation is still being acknowledged; it does not prove the report child has exited.
The Sheet monitor must return `403` from the stop endpoint.

## Keep the Legacy Commands

`python main.py` still runs one Sheet report.
`python listener.py` retains the `/predict_weather` Socket Mode command when `SLACK_APP_TOKEN` is configured.
Do not launch the listener twice for the same Slack app.
The default Compose service schedules daily Sheet reports without needing the listener.

## Update and Roll Back

Before updates, stop the weather container and copy the database to a protected backup location, then check out the release commit and rebuild.
Caddy and its certificate volumes can remain running.

```bash
docker compose --env-file .env -f deploy/compose.yml stop weather
docker compose --env-file .env -f deploy/compose.yml run --rm --no-deps --entrypoint python weather -c "import sqlite3; source=sqlite3.connect('/data/monitors.sqlite'); backup=sqlite3.connect('/data/monitors.backup.sqlite'); source.backup(backup); backup.close(); source.close()"
git pull --ff-only
docker compose --env-file .env -f deploy/compose.yml up -d --build weather
```

Keep the backup protected because it contains observing locations and Slack routing.
SQLite's backup API includes committed data still held in the write-ahead log after an interrupted shutdown.
To roll back, check out the previous verified commit and rebuild the weather container.
Preserve the data volume; do not use `down -v` during an update or rollback.
