"""
Entry point: fetch, filter, format, deliver.

Designed to be run from cron / Task Scheduler at 09:00 daily. Exit code
is non-zero on hard failures (no Slack delivery) so cron alerting works.
"""

import glob
import logging
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from config import LOCATIONS
from locations import DROPPED_LOCATIONS
from delivery.slack_file import SlackFileUploadError, upload_png_report
from fetch.windy_screenshot import collect_windy_links
from processing.schedule import is_in_notification_window
from fetch.weather_api import WeatherFetchError, fetch_location_forecast
from formatting.html_formatter import render_html, save_html, screenshot_html
from processing.filter import build_location_report
from processing.recommend import format_slack_recommendation, rank_nights

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("weather_report")


def build_all_reports(locations: List[Dict[str, Any]]) -> "Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]":
    """
    Run the fetch + filter pipeline for every configured location.

    A failure on one location is logged but doesn't kill the whole run —
    we'd rather ship a partial report than nothing.
    
    Returns (reports, api_metadata) where api_metadata contains generation info.
    """
    reports: List[Dict[str, Any]] = []
    api_metadata = None

    for loc in locations:
        name, lat, lon = loc["name"], loc["lat"], loc["lon"]
        logger.info("Fetching forecast for %s (%.3f, %.3f)", name, lat, lon)
        try:
            payload = fetch_location_forecast(lat, lon)
            # Store metadata from first successful API call
            if api_metadata is None:
                api_metadata = {
                    "generation_time_ms": payload.get("generationtime_ms"),
                    "timezone": payload.get("timezone"),
                }
        except WeatherFetchError as exc:
            logger.error("Skipping %s: %s", name, exc)
            reports.append({
                "location": name,
                "days": [],
                "_error": str(exc),
            })
            continue

        report = build_location_report(name, lat, lon, payload)
        logger.info(
            "%s: %d day(s) with qualifying hours",
            name, len(report["days"]),
        )
        reports.append(report)

    return reports, api_metadata


def main() -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    header = f":crescent_moon: *Night-hour weather report* — generated {today}"

    if not LOCATIONS:
        logger.error("No locations loaded from Google Sheet. Please check sheet permissions and try again.")
        return 1

    # Filter to locations whose preferred visit window includes this week
    active_locations = [
        loc for loc in LOCATIONS
        if is_in_notification_window(loc.get("preferred_period", ""))
    ]
    out_of_season = [
        loc for loc in LOCATIONS
        if loc.get("preferred_period", "").strip()
        and not is_in_notification_window(loc.get("preferred_period", ""))
    ]
    logger.info(
        "%d/%d location(s) active for today's report",
        len(active_locations), len(LOCATIONS),
    )
    for loc in active_locations:
        logger.info("  - %s (preferred: %s)", loc["name"], loc.get("preferred_period", "—"))

    if not active_locations:
        logger.info("No locations in notification window today — nothing to send.")
        return 0

    reports, api_metadata = build_all_reports(active_locations)

    ranked = rank_nights(reports)
    if ranked:
        best = ranked[0]
        logger.info("Best night: %s on %s (cloud %.0f%%, wind %.1f m/s — %s)",
                    best["location"], best["date"],
                    best["cloud"], best["wind"], best["label"])

    windy_links = collect_windy_links(ranked, active_locations)

    html = render_html(reports, header=header, ranked_nights=ranked)

    project_dir = os.path.dirname(__file__)
    for old in glob.glob(os.path.join(project_dir, "weather_report_*.html")):
        os.remove(old)
        logger.info("Removed old report: %s", os.path.basename(old))

    html_path = os.path.join(
        project_dir,
        f"weather_report_{datetime.now().strftime('%Y%m%d_%H%M')}.html",
    )
    save_html(reports, html_path, header=header)
    logger.info("HTML report saved → %s", html_path)

    logger.info("Rendering PNG screenshot…")
    png: Optional[bytes] = None
    try:
        png = screenshot_html(html)
        logger.info("Screenshot captured (%d KB)", len(png) // 1024)
    except Exception as exc:
        logger.warning("Screenshot failed (%s) — will post text-only to Slack", exc)

    rec_text = format_slack_recommendation(reports, dropped=DROPPED_LOCATIONS, out_of_season=out_of_season)

    try:
        upload_png_report(png, title=header, message_text=rec_text or None, windy_links=windy_links)
    except SlackFileUploadError as exc:
        logger.error("Slack PNG upload failed: %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
