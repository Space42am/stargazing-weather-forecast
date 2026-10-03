"""Shared weather calculation, rendering, and delivery adapters."""

import logging
from datetime import date, datetime
from typing import Any, Callable, Optional

import locations as sheet_locations
from delivery.slack_file import upload_png_report
from fetch.weather_api import WeatherFetchError, fetch_location_forecast
from fetch.windy_screenshot import collect_windy_links
from formatting.html_formatter import render_html, screenshot_html
from processing.filter import build_location_report
from processing.recommend import format_slack_recommendation, rank_nights
from processing.schedule import is_in_notification_window

logger = logging.getLogger(__name__)
CancellationCheck = Optional[Callable[[], bool]]


class ReportCancelled(RuntimeError):
    """Raised when a caller stops a report before notification delivery."""


def _check_cancelled(cancelled: CancellationCheck) -> None:
    """Stop work when its caller has cancelled the report.

    Parameters
    ----------
    cancelled : callable or None
        Callback returning whether this report has been stopped.

    Raises
    ------
    ReportCancelled
        If cancellation has been requested.
    """
    if cancelled is not None and cancelled():
        raise ReportCancelled("Weather report was stopped")


def _date_key(value: Optional[date | str]) -> Optional[str]:
    """Normalize an optional event date for filtering forecast days.

    Parameters
    ----------
    value : date, str, or None
        Event boundary as a date or ISO calendar date.

    Returns
    -------
    str or None
        ISO date suitable for chronological comparison.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return date.fromisoformat(value).isoformat()


def build_all_reports(
    locations: list[dict[str, Any]],
    *,
    cancelled: CancellationCheck = None,
    start_date: Optional[date | str] = None,
    end_date: Optional[date | str] = None,
) -> tuple[list[dict[str, Any]], Optional[dict[str, Any]]]:
    """Fetch and process each supplied location without changing weather rules.

    Parameters
    ----------
    locations : list of dict
        Locations containing name, latitude ``lat``, and longitude ``lon``.
    cancelled : callable or None, optional
        Cancellation callback checked before and after every fetch.
    start_date, end_date : date, str, or None, optional
        Inclusive event boundaries applied after weather processing.

    Returns
    -------
    reports : list of dict
        Existing report schema, including per-location fetch errors.
    api_metadata : dict or None
        Generation time and timezone from the first successful fetch.
    """
    start, end = _date_key(start_date), _date_key(end_date)
    if start is not None and end is not None and start > end:
        raise ValueError("start_date must be on or before end_date")

    reports: list[dict[str, Any]] = []
    api_metadata = None
    for loc in locations:
        _check_cancelled(cancelled)
        name, lat, lon = loc["name"], loc["lat"], loc["lon"]
        logger.info("Fetching forecast for %s (%.3f, %.3f)", name, lat, lon)
        try:
            payload = fetch_location_forecast(lat, lon)
        except WeatherFetchError as exc:
            logger.error("Skipping %s: %s", name, exc)
            reports.append({"location": name, "days": [], "_error": str(exc)})
            continue
        _check_cancelled(cancelled)
        if api_metadata is None:
            api_metadata = {
                "generation_time_ms": payload.get("generationtime_ms"),
                "timezone": payload.get("timezone"),
            }
        report = build_location_report(name, lat, lon, payload)
        report["days"] = [
            day
            for day in report["days"]
            if (start is None or day["date"] >= start) and (end is None or day["date"] <= end)
        ]
        reports.append(report)
    _check_cancelled(cancelled)
    return reports, api_metadata


def execute_report(
    locations: list[dict[str, Any]],
    *,
    channel_id: Optional[str] = None,
    dropped: Optional[list[dict[str, Any]]] = None,
    out_of_season: Optional[list[dict[str, Any]]] = None,
    start_date: Optional[date | str] = None,
    end_date: Optional[date | str] = None,
    cancelled: CancellationCheck = None,
    deliver: bool = True,
    output_path: Optional[str] = None,
    header: Optional[str] = None,
) -> dict[str, Any]:
    """Calculate an explicit-location report and optionally notify Slack.

    Parameters
    ----------
    locations : list of dict
        Explicit locations; this adapter never reads the Sheet.
    channel_id : str or None, optional
        Slack destination; None uses configured default.
    dropped, out_of_season : list of dict or None, optional
        Sheet metadata included in the recommendation when supplied.
    start_date, end_date : date, str, or None, optional
        Inclusive event dates applied to the existing forecast window.
    cancelled : callable or None, optional
        Stop callback checked between fetches and before delivery.
    deliver : bool, optional
        Whether to render an image and send a Slack notification.
    output_path : str or None, optional
        Explicit legacy report path; API reports write no shared files.
    header : str or None, optional
        Optional report title retained for the daily entry point.

    Returns
    -------
    dict
        Recommendation, ranked nights, and the full processed reports.

    Raises
    ------
    ReportCancelled
        If stopped before notification delivery.
    """
    reports, _ = build_all_reports(
        locations,
        cancelled=cancelled,
        start_date=start_date,
        end_date=end_date,
    )
    ranked = rank_nights(reports)
    recommendation = format_slack_recommendation(
        reports,
        dropped=dropped,
        out_of_season=out_of_season,
    )
    result = {"recommendation": recommendation, "ranked_nights": ranked, "reports": reports}
    _check_cancelled(cancelled)
    successful_reports = [report for report in reports if "_error" not in report]
    if (start_date is not None or end_date is not None) and successful_reports:
        if not any(report["days"] for report in successful_reports):
            logger.info("Event dates are outside the available forecast — nothing to send.")
            return result
    if not deliver and output_path is None:
        return result
    html = render_html(reports, header=header, ranked_nights=ranked)
    if output_path is not None:
        with open(output_path, "w", encoding="utf-8") as report_file:
            report_file.write(html)
    if not deliver:
        return result

    _check_cancelled(cancelled)
    png = None
    try:
        png = screenshot_html(html)
    except Exception as exc:
        logger.warning("Screenshot failed (%s) — will post text-only to Slack", exc)
    windy_links = collect_windy_links(ranked, locations)
    _check_cancelled(cancelled)
    upload_png_report(
        png,
        channel_id=channel_id,
        title=header,
        message_text=recommendation or None,
        windy_links=windy_links,
    )
    return result


def execute_sheet_report(
    channel_id: Optional[str] = None,
    cancelled: CancellationCheck = None,
    *,
    output_path: Optional[str] = None,
    header: Optional[str] = None,
) -> dict[str, Any]:
    """Refresh Sheet inputs and run the legacy date-filtered report.

    Parameters
    ----------
    channel_id : str or None, optional
        Slack destination; None uses configured default.
    cancelled : callable or None, optional
        Stop callback checked before and after Sheet loading.
    output_path : str or None, optional
        Explicit path for the daily entry point's saved report.
    header : str or None, optional
        Report title supplied by the daily entry point.

    Returns
    -------
    dict
        Recommendation, ranked nights, and processed reports; all empty
        when no Sheet rows fall in their notification window.

    Raises
    ------
    RuntimeError
        If the Sheet cannot supply any geocoded locations.
    ReportCancelled
        If the caller stops this run.
    """
    _check_cancelled(cancelled)
    locations = sheet_locations.get_locations()
    _check_cancelled(cancelled)
    if not locations:
        raise RuntimeError(
            "No locations loaded from Google Sheet. Check Sheet permissions and location rows."
        )
    active_locations = [
        loc for loc in locations if is_in_notification_window(loc.get("preferred_period", ""))
    ]
    out_of_season = [
        loc
        for loc in locations
        if loc.get("preferred_period", "").strip()
        and not is_in_notification_window(loc.get("preferred_period", ""))
    ]
    if not active_locations:
        logger.info("No locations in notification window today — nothing to send.")
        return {"recommendation": "", "ranked_nights": [], "reports": []}
    return execute_report(
        active_locations,
        channel_id=channel_id,
        dropped=list(sheet_locations.DROPPED_LOCATIONS),
        out_of_season=out_of_season,
        cancelled=cancelled,
        output_path=output_path,
        header=header,
    )
