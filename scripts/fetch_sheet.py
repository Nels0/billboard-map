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


def key_material() -> dict:
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        sys.exit("GOOGLE_SERVICE_ACCOUNT_JSON is not set.")
    if not raw.lstrip().startswith("{"):
        raw = Path(raw).read_text(encoding="utf-8")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        sys.exit(f"GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON: {exc}")


def credentials(info: dict):
    return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)


def explain_failure(response, info: dict) -> str:
    """Turn Google's 403 into the specific thing that needs fixing.

    The two causes look identical from the status code alone, so the response
    body is the only way to tell them apart.
    """
    email = info.get("client_email", "(unknown)")
    project = info.get("project_id", "(unknown)")
    try:
        error = response.json().get("error", {})
    except ValueError:
        error = {}
    message = error.get("message", response.text[:400])
    reasons = {d.get("reason") for d in error.get("details", []) if isinstance(d, dict)}

    if "SERVICE_DISABLED" in reasons or "has not been used in project" in message:
        return (
            "The Sheets API is not enabled on the service account's project.\n"
            f"  project: {project}\n"
            "  enable it: https://console.developers.google.com/apis/api/"
            f"sheets.googleapis.com/overview?project={project}\n"
            "  (then wait a minute for it to propagate)\n\n"
            f"Google said: {message}"
        )

    return (
        "The service account cannot read this sheet.\n"
        f"  service account: {email}\n"
        "  Open the sheet -> Share -> paste that address -> Viewer.\n"
        "  It must be shared with the service account itself; sharing with your\n"
        "  own Google account does not grant it access.\n"
        "  If you only have view access and cannot share it onward, copy the\n"
        "  data into your own sheet with =IMPORTRANGE(...) and point SHEET_ID there.\n\n"
        f"Google said: {message}"
    )


def main() -> None:
    sheet_id = os.environ.get("SHEET_ID")
    if not sheet_id:
        sys.exit("SHEET_ID is not set.")
    cell_range = os.environ.get("SHEET_RANGE", "A:Z")

    info = key_material()
    print(f"authenticating as {info.get('client_email', '?')}")

    creds = credentials(info)
    creds.refresh(google.auth.transport.requests.Request())

    session = google.auth.transport.requests.AuthorizedSession(creds)
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
        f"/values/{cell_range}"
    )
    response = session.get(url, params={"majorDimension": "ROWS"})

    if response.status_code == 403:
        sys.exit(explain_failure(response, info))
    if response.status_code == 404:
        sys.exit(
            "No sheet with that id (404). SHEET_ID must be just the id from the "
            "URL, the part between /d/ and /edit, not the whole URL."
        )
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
