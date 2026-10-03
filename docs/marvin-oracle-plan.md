# Deploy the Weather Monitor and Connect Marvin

## Preserve the Existing Forecast

The service continues using the existing five-day Open-Meteo forecast, GFS/ICON/ECMWF models, evening-hour selection, cloud weighting, night rankings, chart rendering, and Slack delivery.
The configured sun-altitude threshold remains +20 degrees; this differs from the previous README description.
Ranking places windy nights last and then sorts by weighted cloud cover, rather than applying the README's claimed 85/15 composite.
Sheet scheduling and its existing date parser remain unchanged.
The Sheet is an input: the current program does not write forecast results back to it.

## Separate Report Generation From Monitor Ownership

A shared pipeline accepts either freshly loaded Sheet locations or explicit coordinates from Marvin.
The legacy one-shot script and `/predict_weather` listener call this same pipeline.
Configuration imports perform no Sheet or geocoder requests.
Generated report files do not overlap across runs, and supplied labels are escaped in HTML.

A small Flask API and one scheduler process run on Oracle.
SQLite persists monitor configuration, ownership, next run, and latest result.
The protected `sheet` monitor runs every day at 09:00 Asia/Yerevan and refreshes the Sheet each time.
Marvin monitors start immediately, repeat at their requested interval, and optionally expire after the final event date.
An event-date filter selects relevant forecast nights without changing the weather calculations.
The scheduler executes reports serially in separate child processes, keeping Chromium memory bounded.
Stopping a Marvin monitor persists the stop and cancels only that monitor's process.
Already-sent notifications cannot be recalled.
Interrupted runs after a service restart are recorded as uncertain failures and resume at the next interval, avoiding immediate duplicate notifications.

## Expose a Bounded API

| Endpoint | Behavior |
| --- | --- |
| `GET /healthz` | Basic service and scheduler health |
| `GET /v1/monitors` | List monitors, optionally filtered by source |
| `POST /v1/monitors` | Create a Marvin monitor from 1–10 named coordinates |
| `GET /v1/monitors/{id}` | Read configuration, execution status, and latest recommendation |
| `POST /v1/monitors/{id}/stop` | Stop a Marvin monitor; reject Sheet-origin monitors |

All `/v1` endpoints require a bearer token.
Creation requires an idempotency key; identical retries return the original monitor, while different content under the same key is rejected.
Coordinates, labels, channels, event dates, and repeat intervals are validated.
The server assigns ownership; callers cannot supply or change it.

## Connect Marvin Through Its Existing Tool System

The optional HTTPS gateway uses `SPACE42_WEATHER_BASE_URL` and `SPACE42_WEATHER_API_TOKEN`.
Marvin receives start, list, status, and stop tools with read-back receipts.
Notification channels and retry identities come from trusted Slack request context.
The model does not choose an arbitrary Slack destination or secret.
The supervisor executes requested starts and stops; specialists can read forecast status.
Existing connector approval policies continue to apply to their own operations.

## Run on Oracle

Use a dedicated Linux instance in the tenancy's home region, within an explicitly verified Always Free allocation when capacity permits.
Run the weather container with a persistent SQLite volume and restart policy.
Terminate HTTPS at Caddy and keep the application port internal.
Keep runtime credentials outside Git and bind the API token to Marvin's worker through GCP Secret Manager.
Keep the legacy Socket Mode listener optional to avoid duplicate slash-command handling.
The service does not require Redis, a separate task queue, or another database server.

## Verify and Release

Regression fixtures compare the existing forecast selection and rankings.
API tests cover authentication, validation, idempotency, restart persistence, event expiry, and rejection of Sheet cancellation.
Pipeline tests exercise partial forecast errors, image fallback, cancellation before delivery, and HTML escaping.
Marvin gateway tests verify trusted destinations, stable retries, and create/stop readback.
After these checks pass, merge the weather branch to `main` and Marvin branch to `master`, push both, deploy Oracle and Marvin, and independently read back runtime revisions and API health.
Live notification verification uses only the intended Slack destination.
