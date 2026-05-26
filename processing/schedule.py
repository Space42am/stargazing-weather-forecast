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


_ARMENIAN_MONTHS = {
    # Armenian month names (genitive and nominative) → English.
    # Codepoints verified against actual sheet data.
    # May: Մ(544) ա(561) յ(575) ր(580) ի(56b) ս(57d) ի(56b)
    'Մայրիսի': 'May',   # Մayrisii (genitive)
    'Մայրիս':       'May',   # Мayris (nominative)
    # June: հ(570) ո(578) ւ(582) ն(576) ի(56b) ս(57d) ի(56b)
    'հունիսի': 'June',  # Хounisii (genitive)
    'հունիս':       'June',  # Хounis (nominative)
}


def _normalize_armenian(text: str) -> str:
    for arm, eng in _ARMENIAN_MONTHS.items():
        text = text.replace(arm, eng)
    return text


def _extract_dates(text: str) -> List[date]:
    # Split on range separators (dash variants, underscore, or " to "), then
    # translate any Armenian month names and parse each part.
    # No fuzzy=True: ambiguous text like "until May 22" must still return zero
    # dates so those locations fall back to always-active.
    parts = re.split(r"\s*[-–—_]\s*|\s+to\s+", text.strip(), maxsplit=1)
    result = []
    for part in parts:
        try:
            result.append(date_parser.parse(_normalize_armenian(part.strip()), dayfirst=False).date())
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
