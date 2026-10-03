"""Read private Google Sheet rows with a narrowly scoped service account."""

import json
from urllib.parse import quote

import requests
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

READONLY_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
TOKEN_URI = "https://oauth2.googleapis.com/token"
READ_TIMEOUT_SECONDS = 10
COLUMN_COUNT = 5
FIRST_COLUMN_INDEX = 8


class SheetReadError(RuntimeError):
    """Represent a safe Sheet error without provider bodies or credentials."""


def _get_json(session: AuthorizedSession, url: str, params: dict) -> dict:
    """Read one Sheets API response without reflecting sensitive error details.

    Parameters
    ----------
    session : AuthorizedSession
        Session holding read-only service-account credentials.
    url : str
        Google Sheets API endpoint.
    params : dict
        Metadata or value-rendering request parameters.

    Returns
    -------
    dict
        Decoded API response.

    Raises
    ------
    SheetReadError
        If authentication, transport, status or response parsing fails.
    """
    try:
        response = session.get(url, params=params, timeout=READ_TIMEOUT_SECONDS)
        status = response.status_code
        if status == 403:
            raise SheetReadError(
                "Private Google Sheet access denied (HTTP 403). Share the Sheet with the "
                "configured service account as Viewer and enable the Google Sheets API."
            )
        if not 200 <= status < 300:
            raise SheetReadError(f"Private Google Sheet read failed (HTTP {status}).")
        data = response.json()
    except (requests.RequestException, GoogleAuthError):
        raise SheetReadError("Private Google Sheet authentication or connection failed.") from None
    except ValueError:
        raise SheetReadError("Google Sheets returned invalid response data.") from None
    if not isinstance(data, dict):
        raise SheetReadError("Google Sheets returned invalid response data.")
    return data


def read_private_sheet_rows(
    spreadsheet_id: str, gid: str, credentials_json: str
) -> list[list[str]]:
    """Read formatted I:M values from the tab selected by its existing numeric GID.

    Parameters
    ----------
    spreadsheet_id : str
        Spreadsheet identifier already used by the public CSV loader.
    gid : str
        Numeric tab identifier already used by the public CSV loader.
    credentials_json : str
        Runtime service-account credential JSON; never logged or returned.

    Returns
    -------
    list[list[str]]
        Formatted I:M values with blank A:H prefix and padding through column M.

    Raises
    ------
    SheetReadError
        If credentials are invalid, the tab is unavailable or reads fail.
    """
    try:
        info = json.loads(credentials_json)
        if (
            not isinstance(info, dict)
            or info.get("type") != "service_account"
            or info.get("token_uri") != TOKEN_URI
        ):
            raise ValueError
        credentials = service_account.Credentials.from_service_account_info(
            info, scopes=(READONLY_SCOPE,)
        )
    except Exception:
        raise SheetReadError(
            "GOOGLE_SERVICE_ACCOUNT_JSON must contain valid Google service-account credentials."
        ) from None
    try:
        sheet_id = int(gid)
        if sheet_id < 0:
            raise ValueError
    except (ValueError, TypeError):
        raise SheetReadError(
            "WEATHER_SHEET_GID must identify a numeric Google Sheet tab."
        ) from None

    base_url = "https://sheets.googleapis.com/v4/spreadsheets/" + quote(spreadsheet_id, safe="")
    with AuthorizedSession(credentials, refresh_timeout=READ_TIMEOUT_SECONDS) as session:
        metadata = _get_json(session, base_url, {"fields": "sheets.properties"})
        sheets = metadata.get("sheets", [])
        if not isinstance(sheets, list):
            raise SheetReadError("Google Sheets returned invalid tab metadata.")
        title = next(
            (
                sheet.get("properties", {}).get("title")
                for sheet in sheets
                if isinstance(sheet, dict)
                and isinstance(sheet.get("properties"), dict)
                and sheet["properties"].get("sheetId") == sheet_id
            ),
            None,
        )
        if not isinstance(title, str) or not title:
            raise SheetReadError(
                "Configured Google Sheet tab was not found. Check WEATHER_SHEET_GID."
            )
        selected_range = "'" + title.replace("'", "''") + "'!I:M"
        data = _get_json(
            session,
            base_url + "/values/" + quote(selected_range, safe=""),
            {"majorDimension": "ROWS", "valueRenderOption": "FORMATTED_VALUE"},
        )
    values = data.get("values", [])
    if not isinstance(values, list) or any(not isinstance(row, list) for row in values):
        raise SheetReadError("Google Sheets returned invalid row data.")
    return [
        [""] * FIRST_COLUMN_INDEX
        + ["" if cell is None else str(cell) for cell in row[:COLUMN_COUNT]]
        + [""] * max(0, COLUMN_COUNT - len(row))
        for row in values
    ]
