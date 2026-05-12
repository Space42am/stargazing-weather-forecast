# Stargazing Weather Forecast Bot

A daily weather forecast bot for astrophotography and stargazing planning.  
Fetches multi-model forecasts, scores each night by cloud cover and wind, and delivers a visual report to Slack every morning.

---

## What it does

- Pulls observing locations dynamically from a Google Sheet, with date-based scheduling per location
- Fetches forecasts from [Open-Meteo](https://open-meteo.com/) using three models: **ECMWF**, **ICON**, and **GFS**
- Filters hours by sun altitude — only nighttime hours are included
- Renders an interactive **Chart.js** HTML report with cloud cover, temperature, and wind charts per location
- Screenshots the report to PNG and delivers it to a Slack channel
- Scores each night using model-weighted cloud cover and wind, then posts per-day recommendations
- Attaches **Windy.com cloud-cover links** for each day with a Good or Excellent forecast
- Responds to the `/predict_weather` Slack slash command on demand

---

## Architecture

```
main.py               — daily scheduled entry point
listener.py           — Slack Socket Mode bot (/predict_weather command)
config.py             — all tunables and env var loading
locations.py          — Google Sheets location loader

fetch/
  weather_api.py      — Open-Meteo API client
  windy_screenshot.py — Windy.com link generator for best nights

processing/
  filter.py           — sun altitude filter, data normalisation
  schedule.py         — date-based notification window filter
  sun.py              — sun altitude calculation (astral)
  recommend.py        — night scoring and Slack recommendation formatter

formatting/
  html_formatter.py   — Chart.js HTML report + Playwright PNG screenshot

delivery/
  slack_file.py       — Slack message + imgbb image hosting
```

---

## Setup

### 1. Install dependencies

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure environment

Copy `.env.example` to `.env` and fill in the values:

```env
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_CHANNEL_ID=C...
IMGBB_API_KEY=...
```

### 3. Google Sheet

The bot reads locations from a Google Sheet. Set your sheet ID in `locations.py`.  
The sheet must have:

| Column | Index | Content |
|---|---|---|
| I | 8 | Preferred visit period (date or date range — see Scheduling) |
| L | 11 | Location name |
| M | 12 | Province name |

Data starts from row 2.

---

## Running

### In VS Code

Press **F5** — the included launch configuration runs `main.py` with the correct venv and UTF-8 encoding automatically.

### From the terminal

```powershell
.\run.ps1
```

Or activate the venv manually first:

```powershell
.venv\Scripts\activate
python main.py
```

**Interactive Slack bot** (run continuously):
```powershell
.venv\Scripts\activate
python listener.py
```

---

## Scheduling

Each location in the sheet can have an optional **preferred visit period** in column I. The bot uses this to skip locations outside their active window and avoid noise.

| Column I value | Behaviour |
|---|---|
| Empty | Always included |
| Single date (`2026-05-22`) | Active from 1 week before that date onward |
| Date range (`2026-05-01 - 2026-05-22`) | Active from 1 week before the start through the end date |
| Unrecognised text | Always included |

---

## Scoring

Each night is scored per location:

| Factor | Weight |
|---|---|
| Cloud cover (weighted: low 60%, mid 30%, high 10%) | 85% |
| Wind speed | 15% |

Model trust (cloud prediction): **ECMWF 50% · ICON 30% · GFS 20%**

Wind above **4 m/s** overrides the label to **Windy** regardless of cloud.

| Label | Cloud score |
|---|---|
| Excellent | < 10% |
| Good | < 20% |
| Fair | < 55% |
| Poor | ≥ 55% |

---

## Slack output

Each morning the bot posts:
- A **text recommendation** listing the best location for each night across the forecast window, with cloud %, wind speed, min temperature, and a rating
- An **inline chart image** showing cloud cover (per model, per layer), temperature, and wind speed for each location
- **Windy.com links** for each day with a Good or Excellent forecast, linking directly to the cloud cover view at that location
