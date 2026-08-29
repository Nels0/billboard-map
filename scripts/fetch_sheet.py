"""Pull the response sheet down as CSV using a read-only service account.

A service account is used rather than Sheet's "publish to web" because the
published-CSV route makes the whole sheet readable to anyone who guesses the
link, and this sheet holds home addresses.

Set SHEET_ID and GOOGLE_SERVICE_ACCOUNT_JSON (the key file's contents, or a path
to it), then share the sheet with the service account's email as a Viewer.
"""

import csv
import io
import json
import os
import sys
from pathlib import Path

import google.auth.transport.requests
from google.oauth2 import service_account

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "responses.csv"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


def credentials():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        sys.exit("GOOGLE_SERVICE_ACCOUNT_JSON is not set.")
    if not raw.lstrip().startswith("{"):
        raw = Path(raw).read_text(encoding="utf-8")
    return service_account.Credentials.from_service_account_info(
        json.loads(raw), scopes=SCOPES
    )


def main() -> None:
    sheet_id = os.environ.get("SHEET_ID")
    if not sheet_id:
        sys.exit("SHEET_ID is not set.")
    cell_range = os.environ.get("SHEET_RANGE", "A:Z")

    creds = credentials()
    creds.refresh(google.auth.transport.requests.Request())

    session = google.auth.transport.requests.AuthorizedSession(creds)
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
        f"/values/{cell_range}"
    )
    response = session.get(url, params={"majorDimension": "ROWS"})
    response.raise_for_status()
    rows = response.json().get("values", [])
    if not rows:
        sys.exit("Sheet returned no rows.")

    # The API truncates trailing empty cells, so pad every row to the header.
    width = max(len(r) for r in rows)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    for row in rows:
        writer.writerow(row + [""] * (width - len(row)))

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(buffer.getvalue(), encoding="utf-8")
    print(f"fetched {len(rows) - 1} rows x {width} cols -> {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
