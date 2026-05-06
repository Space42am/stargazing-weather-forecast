# Stargazing Weather Forecast Bot

A daily weather forecast bot for astrophotography and stargazing planning.  
Fetches multi-model forecasts, scores each night by cloud cover and wind, and delivers a visual report to Slack every morning.

---

## What it does

- Pulls observing locations dynamically from a Google Sheet
- Fetches forecasts from [Open-Meteo](https://open-meteo.com/) using three models: **ECMWF**, **ICON**, and **GFS**
- Filters hours by sun altitude — only nighttime hours are included
- Renders an interactive **Chart.js** HTML report with cloud cover, temperature, and wind charts per location
- Screenshots the report to PNG and delivers it to a Slack channel
- Scores each night using model-weighted cloud cover and wind, then posts per-day recommendations
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

processing/
  filter.py           — sun altitude filter, data normalisation
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
The sheet must have **location name** in column L and **province** in column M, starting from row 2.

---

## Running

**Daily scheduled report** (run via Task Scheduler at 09:00):
```bash
python main.py
```

**Interactive Slack bot** (run continuously):
```bash
python listener.py
```

---

## Scoring

Each night is scored per location:

| Factor | Weight |
|---|---|
| Cloud cover (weighted: low 60%, mid 30%, high 10%) | 85% |
| Wind speed | 15% |

Model trust (cloud prediction): **ECMWF 50% · ICON 30% · GFS 20%**

Wind above **10 m/s** overrides the label to **Windy** regardless of cloud.

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
