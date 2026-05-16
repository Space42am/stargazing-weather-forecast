"""
Generate Windy.com cloud-cover links for the best-ranked night per day.
"""

from typing import Any, Dict, List, Tuple

_GOOD_LABELS = {"Excellent", "Good"}


def collect_windy_links(
    ranked: List[Dict[str, Any]],
    locations: List[Dict[str, Any]],
) -> List[Tuple[str, str]]:
    """
    For each date in the forecast, return (title, windy_url) for the
    best-ranked Good/Excellent location that day.
    """
    loc_by_name = {loc["name"]: loc for loc in locations}
    seen_dates: set = set()
    links: List[Tuple[str, str]] = []

    for night in ranked:
        if night["date"] in seen_dates:
            continue
        if night["label"] not in _GOOD_LABELS:
            continue

        seen_dates.add(night["date"])
        loc_cfg = loc_by_name.get(night["location"])
        if not loc_cfg:
            continue

        lat, lon = loc_cfg["lat"], loc_cfg["lon"]
        date_hour = f"{night['windy_date']}-{night['windy_hour']}"
        url   = f"https://www.windy.com/-Clouds-clouds?clouds,{date_hour},{lat:.3f},{lon:.3f},10,i:pressure,p:cities"
        title = f"{night['location']} — {night['date']} ({night['label']}, cloud {night['cloud']:.0f}%)"
        links.append((title, url))

    return links
