"""
Date-based notification scheduling for location visits.

Three cases:

  Season name  (Аshun / Garun / Аmar / Dzmer in Armenian)
      -> active from 1 week before the season starts, no hard end date.
         The entry is removed from the sheet manually once the visit is done.

  Deadline     (e.g. "until May 22" in Armenian)
      -> active until the deadline date this year. After that, silent.
         Update the sheet to change the deadline.

  Empty / unrecognisable
      -> always active.
"""

import re
from datetime import date
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# Armenian string constants (Unicode codepoints verified against sheet data)
# ---------------------------------------------------------------------------

def _arm(*codepoints: int) -> str:
    return "".join(chr(c) for c in codepoints)

# Season names (lowercase) -> notification start (month, day)
_SEASON_STARTS: dict = {
    _arm(0x0561,0x0577,0x0578,0x0582,0x0576): (8,  25),  # աshun  = Autumn -> Aug 25
    _arm(0x0563,0x0561,0x0580,0x0578,0x0582,0x0576): (2, 22),  # garun  = Spring -> Feb 22
    _arm(0x0561,0x0574,0x0561,0x057c):         (5,  25),  # amar   = Summer -> May 25
    _arm(0x0571,0x0574,0x0565,0x0580):         (11, 24),  # dzmer  = Winter -> Nov 24
}

# Month names in genitive form (as used in "May 22nd") -> month number
_MONTH_MAP: dict = {
    _arm(0x0570,0x0578,0x0582,0x0576,0x057e,0x0561,0x0580,0x056b): 1,   # hounvari
    _arm(0x0583,0x0565,0x057f,0x0580,0x057e,0x0561,0x0580,0x056b): 2,   # phetrvari
    _arm(0x0574,0x0561,0x0580,0x057f,0x056b):                       3,   # marti
    _arm(0x0561,0x057a,0x0580,0x056b,0x056c,0x056b):                4,   # aprili
    _arm(0x0574,0x0561,0x0575,0x056b,0x057d,0x056b):                5,   # mayisi
    _arm(0x0570,0x0578,0x0582,0x0576,0x056b,0x057d,0x056b):         6,   # hounisi
    _arm(0x0570,0x0578,0x0582,0x056c,0x056b,0x057d,0x056b):         7,   # houlisi
    _arm(0x0585,0x0563,0x0578,0x057d,0x057f,0x0578,0x057d,0x056b):  8,   # ogostosi
    _arm(0x057d,0x0565,0x057a,0x057f,0x0565,0x0574,0x0562,0x0565,0x0580,0x056b): 9,   # septembeyi
    _arm(0x0570,0x0578,0x056f,0x057f,0x0565,0x0574,0x0562,0x0565,0x0580,0x056b): 10,  # hoktembeyi
    _arm(0x0576,0x0578,0x0575,0x0565,0x0574,0x0562,0x0565,0x0580,0x056b):        11,  # noyemberi
    _arm(0x0564,0x0565,0x056f,0x0565,0x0574,0x0562,0x0565,0x0580,0x056b):        12,  # dekemberi
}


def _parse_deadline(text: str) -> Optional[Tuple[int, int]]:
    """
    Extract (month, day) from a deadline string like 'until May 22'.
    Returns None if no month name is found.
    """
    for month_word, month_num in _MONTH_MAP.items():
        if month_word in text:
            day_match = re.search(r"\d{1,2}", text)
            day = int(day_match.group()) if day_match else 1
            try:
                date(2000, month_num, day)   # validate
            except ValueError:
                day = 1
            return (month_num, day)
    return None


def is_in_notification_window(preferred_period: str, today: Optional[date] = None) -> bool:
    """
    Return True if this location should be included in today's report.

    Season entries are held back until 1 week before their season starts.
    Deadline entries are active from Jan 1 through the deadline date.
    Everything else is always included.
    """
    if not preferred_period or not preferred_period.strip():
        return True

    text = preferred_period.strip().lower()

    if today is None:
        today = date.today()

    # --- Season check ---
    for season_text, (start_m, start_d) in _SEASON_STARTS.items():
        if season_text not in text:
            continue

        # Active once this year's notification date arrives.
        try:
            if today >= date(today.year, start_m, start_d):
                return True
        except ValueError:
            pass

        # Winter (Nov 24) spans the Dec->Jan boundary; check previous year
        # for the Jan-Mar window.
        if start_m >= 11 and today.month <= 3:
            try:
                if today >= date(today.year - 1, start_m, start_d):
                    return True
            except ValueError:
                pass

        return False

    # --- Deadline check ---
    deadline = _parse_deadline(text)
    if deadline is not None:
        month, day = deadline
        try:
            target = date(today.year, month, day)
        except ValueError:
            return False
        return today <= target

    # Unrecognised text -> always include
    return True
