"""
Date-based notification scheduling.

Column I value cases:
  Single date   → active from 1 week before that date, no end.
  Date range    → active from 1 week before start through end date.
  Text / empty  → always active (no scheduling constraint).
"""

import logging
import re
from datetime import date, timedelta
from typing import List, Optional

from dateutil import parser as date_parser

logger = logging.getLogger(__name__)


def _extract_dates(text: str) -> List[date]:
    # Split on range separators first, then try strict parse of each part.
    # No fuzzy=True: text like "until May 22" or Armenian strings must return
    # zero dates so those locations stay always-active.
    parts = re.split(r"\s*[-–—]\s*|\s+to\s+", text.strip(), maxsplit=1)
    result = []
    for part in parts:
        try:
            result.append(date_parser.parse(part.strip(), dayfirst=False).date())
        except Exception:
            pass
    logger.debug("Extracted dates from %r: %s", text, result)
    return result


def is_in_notification_window(preferred_period: str, today: Optional[date] = None) -> bool:
    if today is None:
        today = date.today()

    if not preferred_period or not preferred_period.strip():
        return True

    dates = _extract_dates(preferred_period.strip())

    if not dates:
        return True  # unrecognised text → always include

    start = dates[0]
    end   = dates[1] if len(dates) > 1 else None

    if today < start - timedelta(days=7):
        return False
    if end is not None and today > end:
        return False
    return True
