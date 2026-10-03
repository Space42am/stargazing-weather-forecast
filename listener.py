"""
Slack Socket Mode listener.

Responds to the /weather slash command by running the full forecast pipeline
and posting the results into whichever channel the command was typed in.

Run this process continuously (e.g. via Task Scheduler at startup):
    python listener.py
"""

import logging
import threading

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from config import SLACK_APP_TOKEN, SLACK_BOT_TOKEN
from pipeline import execute_sheet_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("weather_listener")

app = App(token=SLACK_BOT_TOKEN)


def _run_pipeline(channel_id: str) -> None:
    """Refresh Sheet inputs and post the report to a channel.

    Parameters
    ----------
    channel_id : str
        Slack channel where the slash command was invoked.
    """
    execute_sheet_report(channel_id=channel_id)


@app.command("/predict_weather")
def handle_weather(ack, say, command):
    """Acknowledge the slash command and start its report in a thread.

    Parameters
    ----------
    ack, say : callable
        Slack acknowledgement and channel message callbacks.
    command : dict
        Slack slash-command payload containing the channel identifier.
    """
    ack()  # must respond within 3 s — acknowledge first, then do the work
    say(":hourglass_flowing_sand: Fetching forecast, give me a moment…")
    channel_id = command["channel_id"]
    threading.Thread(
        target=_safe_run,
        args=(channel_id, say),
        daemon=True,
    ).start()


def _safe_run(channel_id: str, say) -> None:
    """Report pipeline errors to the invoking Slack channel.

    Parameters
    ----------
    channel_id : str
        Destination of the report.
    say : callable
        Slack callback used to report failures.
    """
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
