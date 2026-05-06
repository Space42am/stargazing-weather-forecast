"""
Score each (location, night) pair and return ranked recommendations.

All weight dicts use relative values — they are normalized at scoring time,
so you can change individual values without worrying about the sum.

Tuning knobs at the top of this file:
  MODEL_WEIGHTS       — per-model cloud prediction reliability
  CLOUD_LAYER_WEIGHTS — relative importance of each altitude layer
  WIND_THRESHOLD      — m/s above which a night is vetoed as "Windy"
"""

from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Tunable parameters
# ---------------------------------------------------------------------------

# Cloud forecast reliability by model for this region.
# ECMWF outperforms GFS over the Caucasus; ICON sits in between.
# Unknown models fall back to MODEL_WEIGHT_DEFAULT.
MODEL_WEIGHTS: Dict[str, float] = {
    "ECMWF": 0.50,
    "ICON":  0.30,
    "GFS":   0.20,
}
MODEL_WEIGHT_DEFAULT: float = 0.20

# Importance of each cloud altitude layer in the composite cloud score.
# Low cloud blocks views directly; high cirrus is mostly transparent.
CLOUD_LAYER_WEIGHTS: Dict[str, float] = {
    "low":  0.60,
    "mid":  0.30,
    "high": 0.10,
}

WIND_THRESHOLD: float = 10.0  # m/s — above this the night is vetoed as "Windy"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _cloud_score(low: float, mid: float, high: float) -> float:
    """Weighted cloud score for a single model reading. Auto-normalises layer weights."""
    lw = CLOUD_LAYER_WEIGHTS
    total_w = lw["low"] + lw["mid"] + lw["high"]
    return (lw["low"] * low + lw["mid"] * mid + lw["high"] * high) / total_w


def _night_scores(day: Dict[str, Any]) -> Optional[Dict[str, Optional[float]]]:
    """
    Return mean weighted cloud score, mean wind, and minimum temperature for the night.
    Each hour's cloud score is a model-weighted average (ECMWF trusted most).
    Min temp = average of each model's coldest hour across the night.
    """
    cloud_vals: List[float] = []
    wind_vals:  List[float] = []

    # Collect per-model temperature series to find each model's night minimum
    model_temps: Dict[str, List[float]] = {}

    for entry in day.get("entries", []):
        models = entry.get("models") or {}
        if not models:
            continue

        # Model-weighted cloud score for this hour
        c_sum = w_sum = 0.0
        for model_name, mdata in models.items():
            w    = MODEL_WEIGHTS.get(model_name, MODEL_WEIGHT_DEFAULT)
            low  = mdata.get("cloud_low")  or 0
            mid  = mdata.get("cloud_mid")  or 0
            high = mdata.get("cloud_high") or 0
            c_sum += w * _cloud_score(low, mid, high)
            w_sum += w

            temp = mdata.get("temp")
            if temp is not None:
                model_temps.setdefault(model_name, []).append(float(temp))

        if w_sum:
            cloud_vals.append(c_sum / w_sum)

        # Wind: simple average across models
        for mdata in models.values():
            wind = mdata.get("wind")
            if wind is not None:
                wind_vals.append(float(wind))

    if not cloud_vals:
        return None

    # Average each model's minimum temperature across models
    model_mins = [min(temps) for temps in model_temps.values() if temps]
    min_temp = round(sum(model_mins) / len(model_mins), 1) if model_mins else None

    return {
        "cloud":    round(sum(cloud_vals) / len(cloud_vals), 1),
        "wind":     round(sum(wind_vals)  / len(wind_vals),  1) if wind_vals else 0.0,
        "min_temp": min_temp,
    }


def _label(cloud: float, wind: float) -> str:
    if wind >= WIND_THRESHOLD:  return "Windy"
    if cloud < 10:              return "Excellent"
    if cloud < 20:              return "Good"
    if cloud < 55:              return "Fair"
    return "Poor"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def rank_nights(location_reports: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """All (location, date) pairs sorted best → worst. Windy nights sink to the bottom."""
    ranked = []
    for report in location_reports:
        loc = report.get("location", "?")
        for day in report.get("days", []):
            s = _night_scores(day)
            if s is None:
                continue
            label = _label(s["cloud"] or 0.0, s["wind"] or 0.0)
            ranked.append({
                "location": loc,
                "date":     day["date"],
                "cloud":    s["cloud"],
                "wind":     s["wind"],
                "min_temp": s["min_temp"],
                "label":    label,
            })

    ranked.sort(key=lambda x: (x["label"] == "Windy", x["cloud"]))
    return ranked


def format_slack_recommendation(location_reports: List[Dict[str, Any]]) -> str:
    """
    For every night in the forecast window, list ALL locations ranked best→worst.
    Output is in chronological date order so it reads as a trip planner.
    """
    all_nights = rank_nights(location_reports)
    if not all_nights:
        return ""

    by_date: Dict[str, List[Dict[str, Any]]] = {}
    for n in all_nights:
        by_date.setdefault(n["date"], []).append(n)

    _EMOJI = {
        "Excellent": ":star2:",
        "Good":      ":white_check_mark:",
        "Fair":      ":partly_sunny:",
        "Poor":      ":cloud:",
        "Windy":     ":wind_blowing_face:",
    }

    lines = [":telescope: *Stargazing forecast — all locations, all nights*\n"]
    for i, d in enumerate(sorted(by_date)):
        tonight = " _(tonight)_" if i == 0 else ""
        lines.append(f"*{d}*{tonight}")
        for n in by_date[d]:
            em = _EMOJI.get(n["label"], "")
            temp_str = f"  ·  🌡 {n['min_temp']:.0f}°C" if n["min_temp"] is not None else ""
            lines.append(
                f"  {em} *{n['location']}*  —  "
                f"cloud {n['cloud']:.0f}%  ·  wind {n['wind']:.1f} m/s{temp_str}  ·  *{n['label']}*"
            )
        lines.append("")

    return "\n".join(lines).strip()
