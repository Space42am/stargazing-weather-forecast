"""Daily Sheet report entry point for cron or Task Scheduler."""

import logging
import os
import sys
from datetime import datetime

from delivery.slack_file import SlackFileUploadError
from pipeline import build_all_reports as build_all_reports
from pipeline import execute_sheet_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("weather_report")


def main() -> int:
    """Refresh the Sheet, save the daily report, and deliver to Slack.

    Returns
    -------
    int
        Zero for success or no active locations; one for failed input or
        Slack delivery.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    header = f":crescent_moon: *Night-hour weather report* — generated {today}"
    html_path = os.path.join(os.path.dirname(__file__), "weather_report_daily.html")
    try:
        execute_sheet_report(output_path=html_path, header=header)
    except (RuntimeError, SlackFileUploadError) as exc:
        logger.error("Weather report failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
