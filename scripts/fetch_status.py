"""Read the billboard status tab into a build-time snapshot.

This is the *fallback* read path. The map fetches live status straight from the
Apps Script endpoint, so this snapshot only has to cover the gap when that
endpoint is unreachable - a phone with no signal, a hit quota, a script mid
re-deploy. A pin showing no status at all is indistinguishable from a sign
nobody has touched yet, and that ambiguity is what sends someone to the same
address twice.

Because it is a fallback, nothing here is fatal: every failure prints a loud
warning and writes an empty snapshot rather than breaking the map build. The
status feature can therefore be merged before the Google side exists.

Reads with the same read-only service account as fetch_sheet.py. All writes go
through the Apps Script deployment, which runs as its owner - the service
account never needs more than Viewer on the status spreadsheet.
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "build" / "status.plain.json"

DEFAULT_TAB = "status"

# Mirrors STATUS_HEADERS in scripts/apps_script/Code.gs.
COL_ID = 0
COL_STREET = 1
COL_SUBURB = 2
COL_STATE = 3
COL_UPDATED = 4
COL_BY = 5
COL_NOTE = 6


def warn(message: str) -> None:
    """Loud on a terminal, and folded into the run summary on Actions."""
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::warning title=Billboard status::{message}")
    print(f"WARNING: {message}", file=sys.stderr)


def write(snapshot: dict) -> None:
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")


def cell(row: list[str], idx: int) -> str:
    return row[idx].strip() if idx < len(row) else ""


def main() -> None:
    # The status tabs may live in the response spreadsheet or in one of their
    # own, so STATUS_SHEET_ID falls back to SHEET_ID.
    sheet_id = os.environ.get("STATUS_SHEET_ID") or os.environ.get("SHEET_ID")
    tab = os.environ.get("STATUS_TAB") or DEFAULT_TAB

    if not sheet_id:
        print("no STATUS_SHEET_ID or SHEET_ID; writing an empty status snapshot")
        write({})
        return

    if not os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"):
        warn("GOOGLE_SERVICE_ACCOUNT_JSON is not set; status snapshot will be empty")
        write({})
        return

    # Imported here, not at module scope: the unconfigured path above is the
    # normal state until the status sheet exists, and it should not need the
    # Google libraries to be installed to no-op cleanly.
    import google.auth.transport.requests

    from fetch_sheet import BASE, a1, credentials, key_material

    creds = credentials(key_material())
    creds.refresh(google.auth.transport.requests.Request())
    session = google.auth.transport.requests.AuthorizedSession(creds)

    # Check the tab exists before asking for its values: a missing range comes
    # back as a bare 400 that reads like a malformed request.
    meta = session.get(f"{BASE}/{sheet_id}", params={"fields": "sheets.properties(title)"})
    if meta.status_code != 200:
        warn(
            f"cannot read the status spreadsheet ({meta.status_code}). Share it with "
            "the service account as a Viewer, or unset STATUS_SHEET_ID."
        )
        write({})
        return

    tabs = [s["properties"]["title"] for s in meta.json().get("sheets", [])]
    if tab not in tabs:
        warn(
            f"no tab named {tab!r} (found: {', '.join(tabs)}). The map will still "
            "read live status from the endpoint; only the offline fallback is empty."
        )
        write({})
        return

    response = session.get(
        f"{BASE}/{sheet_id}/values/{a1(tab, 'A:G')}",
        params={"majorDimension": "ROWS"},
    )
    if response.status_code != 200:
        warn(f"status tab read failed ({response.status_code}): {response.text[:200]}")
        write({})
        return

    rows = response.json().get("values", [])[1:]  # drop the header row

    snapshot, odd = {}, []
    for row in rows:
        rid = cell(row, COL_ID)
        if not rid:
            continue
        state = cell(row, COL_STATE).lower()
        # Someone hand-editing the sheet can type anything. Pass it through
        # rather than discarding it; the map shows unrecognised values as a
        # caveat so the typo is visible and fixable.
        if state and state not in {"up", "down"}:
            odd.append(f"{cell(row, COL_STREET) or rid}: {state!r}")
        snapshot[rid] = {
            "state": state,
            "street": cell(row, COL_STREET),
            "suburb": cell(row, COL_SUBURB),
            "updated": cell(row, COL_UPDATED),
            "by": cell(row, COL_BY),
            "note": cell(row, COL_NOTE),
        }

    write(snapshot)

    up = sum(1 for s in snapshot.values() if s["state"] == "up")
    down = sum(1 for s in snapshot.values() if s["state"] == "down")
    print(f"status snapshot: {len(snapshot)} rows -> {OUT.relative_to(ROOT)}")
    print(f"  {up} up / {down} down / {len(snapshot) - up - down} other or unset")
    if odd:
        warn(f"unrecognised state values: {'; '.join(odd[:10])}")


if __name__ == "__main__":
    main()
