"""
Slack Socket Mode listener.

Responds to the /weather slash command by running the full forecast pipeline
and posting the results into whichever channel the command was typed in.

Run this process continuously (e.g. via Task Scheduler at startup):
    python listener.py
"""

import glob
import logging
import os
import threading
from datetime import datetime

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from config import LOCATIONS, SLACK_APP_TOKEN, SLACK_BOT_TOKEN
from locations import DROPPED_LOCATIONS
from delivery.slack_file import SlackFileUploadError, upload_png_report
from fetch.weather_api import WeatherFetchError, fetch_location_forecast
from fetch.windy_screenshot import collect_windy_links
from formatting.html_formatter import render_html, save_html, screenshot_html
from processing.filter import build_location_report
from processing.recommend import format_slack_recommendation, rank_nights
from processing.schedule import is_in_notification_window

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("weather_listener")

app = App(token=SLACK_BOT_TOKEN)


def _run_pipeline(channel_id: str) -> None:
    """Full fetch → render → deliver pipeline, posting into *channel_id*."""
    logger.info("Pipeline triggered for channel %s", channel_id)

    active_locations = [
        loc for loc in LOCATIONS
        if is_in_notification_window(loc.get("preferred_period", ""))
    ]
    logger.info("%d/%d location(s) active", len(active_locations), len(LOCATIONS))

    reports: list = []
    for loc in active_locations:
        name, lat, lon = loc["name"], loc["lat"], loc["lon"]
        try:
            payload = fetch_location_forecast(lat, lon)
            reports.append(build_location_report(name, lat, lon, payload))
            logger.info("%s: fetched", name)
        except WeatherFetchError as exc:
            logger.error("Skipping %s: %s", name, exc)
            reports.append({"location": name, "days": [], "_error": str(exc)})

    ranked      = rank_nights(reports)
    windy_links = collect_windy_links(ranked, active_locations)
    html_str    = render_html(reports, ranked_nights=ranked)

    project_dir = os.path.dirname(__file__)
    for old in glob.glob(os.path.join(project_dir, "weather_report_*.html")):
        os.remove(old)
    html_path = os.path.join(project_dir, f"weather_report_{datetime.now().strftime('%Y%m%d_%H%M')}.html")
    save_html(reports, html_path)

    png      = screenshot_html(html_str)
    rec_text = format_slack_recommendation(reports, dropped=DROPPED_LOCATIONS)

    try:
        upload_png_report(png, channel_id=channel_id, message_text=rec_text or None, windy_links=windy_links)
        logger.info("Results posted to channel %s", channel_id)
    except SlackFileUploadError as exc:
        logger.error("Delivery failed: %s", exc)
        raise


@app.command("/predict_weather")
def handle_weather(ack, say, command):
    ack()  # must respond within 3 s — acknowledge first, then do the work
    say(":hourglass_flowing_sand: Fetching forecast, give me a moment…")
    channel_id = command["channel_id"]
    threading.Thread(
        target=_safe_run,
        args=(channel_id, say),
        daemon=True,
    ).start()


def _safe_run(channel_id: str, say) -> None:
    try:
        _run_pipeline(channel_id)
    except Exception as exc:
        logger.error("Pipeline error: %s", exc, exc_info=True)
        say(f":warning: Something went wrong: {exc}")


if __name__ == "__main__":
    if not SLACK_APP_TOKEN:
        raise SystemExit("SLACK_APP_TOKEN is not set in .env")
    logger.info("Starting Slack weather bot (Socket Mode)…")
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
