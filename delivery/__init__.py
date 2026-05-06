"""
Send messages to Slack via Incoming Webhook.

Single retry on transient failure. We don't go further (exponential
backoff, queueing) because the upstream is a once-a-day cron — if the
second attempt fails, alerting via cron's stderr is the right exit hatch.
"""

import logging
import time
from typing import Optional

import requests

from config import SLACK_RETRY_COUNT, SLACK_TIMEOUT_SECONDS, SLACK_WEBHOOK_URL

logger = logging.getLogger(__name__)


class SlackDeliveryError(Exception):
    """Raised when Slack delivery fails after all retries."""


def send_to_slack(
    text: str,
    webhook_url: Optional[str] = None,
    retries: int = SLACK_RETRY_COUNT,
) -> None:
    """
    POST a message to Slack.

    Args:
        text: the message body (Slack mrkdwn).
        webhook_url: override; defaults to config.SLACK_WEBHOOK_URL.
        retries: extra attempts on top of the first (so 1 = up to 2 tries).

    Raises:
        SlackDeliveryError: if the webhook is unset or all attempts fail.
    """
    url = webhook_url or SLACK_WEBHOOK_URL
    if not url:
        raise SlackDeliveryError(
            "SLACK_WEBHOOK_URL is not configured (env var or config.py)"
        )

    payload = {"text": text}
    last_error: Optional[str] = None
    attempts = retries + 1

    for attempt in range(1, attempts + 1):
        try:
            response = requests.post(
                url,
                json=payload,
                timeout=SLACK_TIMEOUT_SECONDS,
            )
            if response.ok:
                logger.info("Slack message delivered (attempt %d)", attempt)
                return
            last_error = (
                f"HTTP {response.status_code}: {response.text[:200]}"
            )
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"

        logger.warning(
            "Slack delivery attempt %d/%d failed: %s",
            attempt, attempts, last_error,
        )
        if attempt < attempts:
            time.sleep(1.0)  # brief pause before the single retry

    raise SlackDeliveryError(
        f"Slack delivery failed after {attempts} attempt(s): {last_error}"
    )