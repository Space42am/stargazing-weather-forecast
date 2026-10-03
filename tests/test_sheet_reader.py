"""Verify private Sheet reads and preserve the existing public CSV parser."""

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch
from urllib.parse import unquote

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from google.auth.exceptions import RefreshError
from google.oauth2 import service_account

import locations
from sheet_reader import READONLY_SCOPE, TOKEN_URI, SheetReadError, read_private_sheet_rows

FAKE_INFO = {
    "type": "service_account",
    "token_uri": TOKEN_URI,
    "client_email": "weather-reader@example.invalid",
    "private_key": "not-a-real-private-key",
}


class SheetReaderTests(unittest.TestCase):
    """Read mocked API responses without credentials, notifications or network I/O."""

    def response(self, data, status=200):
        """Build a response containing deliberately sensitive-looking failure text.

        Parameters
        ----------
        data : dict
            API response body.
        status : int, optional
            HTTP status code.

        Returns
        -------
        Mock
            Response with controllable JSON and status.
        """
        return Mock(
            status_code=status, json=Mock(return_value=data), text="private-key-and-row-sentinel"
        )

    def test_gid_title_escaping_formatted_rows_and_blank_province(self):
        """Select the existing GID and preserve I/L/M positions when M is omitted."""
        row = ["October 10, 2026", "", "", "Test observing site"]
        session = Mock()
        session.get.side_effect = [
            self.response(
                {
                    "sheets": [
                        {"properties": {"sheetId": 9, "title": "Other"}},
                        {"properties": {"sheetId": 42, "title": "Observer's tab"}},
                    ]
                }
            ),
            self.response({"values": [["Header"], row]}),
        ]
        with (
            patch(
                "sheet_reader.service_account.Credentials.from_service_account_info",
                return_value=Mock(),
            ) as constructor,
            patch("sheet_reader.AuthorizedSession") as authorized,
            redirect_stdout(io.StringIO()) as output,
        ):
            authorized.return_value.__enter__.return_value = session
            rows = read_private_sheet_rows("spreadsheet-test", "42", json.dumps(FAKE_INFO))
        self.assertEqual(constructor.call_args.kwargs["scopes"], (READONLY_SCOPE,))
        self.assertEqual(authorized.call_args.kwargs["refresh_timeout"], 10)
        self.assertEqual(
            session.get.call_args_list[0].kwargs["params"], {"fields": "sheets.properties"}
        )
        self.assertTrue(
            unquote(session.get.call_args_list[1].args[0]).endswith("/values/'Observer''s tab'!I:M")
        )
        self.assertEqual(
            session.get.call_args_list[1].kwargs["params"],
            {"majorDimension": "ROWS", "valueRenderOption": "FORMATTED_VALUE"},
        )
        self.assertEqual([len(entry) for entry in rows], [13, 13])
        self.assertEqual(rows[1][:8], [""] * 8)
        self.assertEqual(rows[1][8], "October 10, 2026")
        self.assertEqual(rows[1][11], "Test observing site")
        self.assertEqual(rows[1][12], "")
        self.assertEqual(output.getvalue(), "")

    def test_auth_constructor_can_sign_with_default_runtime_dependency(self):
        """Use an ephemeral RSA key to verify google-auth's installed signing backend."""
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        info = {
            **FAKE_INFO,
            "private_key": key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ).decode(),
        }
        credentials = service_account.Credentials.from_service_account_info(
            info, scopes=(READONLY_SCOPE,)
        )
        signature = credentials.sign_bytes(b"read-only-adapter-test")
        key.public_key().verify(
            signature, b"read-only-adapter-test", padding.PKCS1v15(), hashes.SHA256()
        )
        self.assertEqual(credentials.scopes, (READONLY_SCOPE,))

    def test_credential_json_type_and_token_uri_fail_safely(self):
        """Reject malformed keys and credential substitutions before any HTTP request."""
        values = [
            "private-key-and-row-sentinel",
            "[]",
            "{}",
            json.dumps({**FAKE_INFO, "type": "authorized_user"}),
            json.dumps({**FAKE_INFO, "token_uri": "https://example.invalid/token"}),
            json.dumps({**FAKE_INFO, "private_key": []}),
            json.dumps(FAKE_INFO),
        ]
        with patch("sheet_reader.AuthorizedSession") as session:
            for value in values:
                with self.subTest(value=value), self.assertRaises(SheetReadError) as error:
                    read_private_sheet_rows("spreadsheet-test", "42", value)
                self.assertEqual(
                    str(error.exception),
                    "GOOGLE_SERVICE_ACCOUNT_JSON must contain valid Google service-account credentials.",
                )
                self.assertTrue(error.exception.__suppress_context__)
        session.assert_not_called()

    def test_safe_access_denied_missing_gid_and_authentication_errors(self):
        """Avoid reflecting Google failure bodies, refresh errors or request details."""
        session = Mock()
        cases = [
            self.response({}, 403),
            self.response({}, 401),
            self.response(
                {
                    "sheets": [
                        {"properties": {"sheetId": 1, "title": "private-key-and-row-sentinel"}}
                    ]
                }
            ),
            RefreshError("private-key-and-row-sentinel"),
            requests.ConnectionError("private-key-and-row-sentinel"),
        ]
        with (
            patch(
                "sheet_reader.service_account.Credentials.from_service_account_info",
                return_value=Mock(),
            ),
            patch("sheet_reader.AuthorizedSession") as authorized,
            redirect_stdout(io.StringIO()) as output,
        ):
            authorized.return_value.__enter__.return_value = session
            for response in cases:
                session.get.side_effect = [response]
                with (
                    self.subTest(kind=type(response).__name__),
                    self.assertRaises(SheetReadError) as error,
                ):
                    read_private_sheet_rows("spreadsheet-test", "42", json.dumps(FAKE_INFO))
                self.assertNotIn("private-key-and-row-sentinel", str(error.exception))
            self.assertEqual(output.getvalue(), "")

    def test_private_rows_use_existing_parser_without_public_csv_fallback(self):
        """Retain original date/location/province interpretation in authenticated mode."""
        row = [""] * 13
        row[8], row[11] = "2026-10-10", "Test observing site"
        with (
            patch.dict(os.environ, {"GOOGLE_SERVICE_ACCOUNT_JSON": "protected-runtime-value"}),
            patch("locations.read_private_sheet_rows", return_value=[[""] * 13, row]) as reader,
            patch("locations.requests.get") as public,
            patch(
                "locations.geocode_location", return_value={"name": row[11], "lat": 40, "lon": 44}
            ) as geocode,
            patch("locations.time.sleep"),
            redirect_stdout(io.StringIO()),
        ):
            result = locations.fetch_locations_from_sheet()
        reader.assert_called_once_with(
            locations.SPREADSHEET_ID, locations.SHEET_GID, "protected-runtime-value"
        )
        public.assert_not_called()
        geocode.assert_called_once_with("Test observing site", "")
        self.assertEqual(result[0]["preferred_period"], "2026-10-10")
        with (
            patch.dict(os.environ, {"GOOGLE_SERVICE_ACCOUNT_JSON": "protected-runtime-value"}),
            patch(
                "locations.read_private_sheet_rows",
                side_effect=SheetReadError("Private Sheet access denied."),
            ),
            patch("locations.requests.get") as public,
            redirect_stdout(io.StringIO()),
        ):
            with self.assertRaisesRegex(locations.LocationError, "Private Sheet access denied"):
                locations.fetch_locations_from_sheet()
        public.assert_not_called()

    def test_default_public_csv_path_and_column_behavior_are_preserved(self):
        """Keep public transport, UTF-8 names and existing short-row handling unchanged."""
        header = ["Header"] * 13
        row = [""] * 13
        row[8], row[11], row[12] = "2026-10-10", "Բյուրական", "Արագածոտն"
        short_row = [""] * 11 + ["No province cell"]
        csv_text = "\n".join(",".join(values) for values in (header, row, short_row))
        response = Mock(content=csv_text.encode(), status_code=200)
        with (
            patch.dict(os.environ, {"GOOGLE_SERVICE_ACCOUNT_JSON": ""}),
            patch("locations.read_private_sheet_rows") as private,
            patch("locations.requests.get", return_value=response) as public,
            patch(
                "locations.geocode_location", return_value={"name": row[11], "lat": 40, "lon": 44}
            ) as geocode,
            patch("locations.time.sleep"),
            redirect_stdout(io.StringIO()),
        ):
            result = locations.fetch_locations_from_sheet()
        private.assert_not_called()
        public.assert_called_once_with(
            locations.CSV_URL,
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            allow_redirects=True,
        )
        geocode.assert_called_once_with("Բյուրական", "Արագածոտն")
        self.assertEqual(result[0]["preferred_period"], "2026-10-10")


if __name__ == "__main__":
    unittest.main()
