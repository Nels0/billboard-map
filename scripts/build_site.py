"""Combine records with geocoded coordinates and publish encrypted map data.

Writes exactly one artefact into site/: an AES-GCM envelope. Nothing readable
leaves this script, because site/ is what gets published to GitHub Pages.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from crypto_util import write_encrypted

ROOT = Path(__file__).resolve().parent.parent
RECORDS = ROOT / "build" / "records.plain.json"
CACHE = ROOT / "build" / "geocode-cache.plain.json"
STATUS = ROOT / "build" / "status.plain.json"
OUT = ROOT / "site" / "data.enc.json"

# Only these reach the browser; anything not listed is dropped even if the
# export grows new columns. Contact details ARE published: the delivery team
# needs to reach the host. That makes the passphrase the only thing standing
# between a public URL and a supporter's name, phone and email, so treat it
# accordingly - see the Privacy section of the README.
PUBLISH_FIELDS = (
    "id",
    "street",
    "suburb",
    "priority",
    "name",
    "email",
    "phone",
    "property_type",
    "sign_location",
    "mounting",
    "poster_size",
    "permission",
    "is_home",
    "notes",
)


def status_config() -> dict | None:
    """Endpoint details for live up/down tracking, or None if not configured.

    These are deliberately baked into the *encrypted* payload rather than into
    index.html. The deployment URL is a bearer capability - anyone holding it
    and the token can write status - so it sits behind the same passphrase as
    the addresses instead of being served in the clear to every visitor.
    """
    endpoint = os.environ.get("STATUS_ENDPOINT", "").strip()
    token = os.environ.get("STATUS_TOKEN", "").strip()
    if not endpoint or not token:
        return None
    if not endpoint.startswith("https://"):
        sys.exit(f"STATUS_ENDPOINT must be https, got {endpoint[:40]!r}")
    return {"endpoint": endpoint, "token": token}


def main() -> None:
    passphrase = os.environ.get("MAP_PASSPHRASE")
    if not passphrase:
        sys.exit("MAP_PASSPHRASE is not set; refusing to build.")

    records = json.loads(RECORDS.read_text(encoding="utf-8"))
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    status = json.loads(STATUS.read_text(encoding="utf-8")) if STATUS.exists() else {}

    features, unplaced = [], []
    for record in records:
        located = cache.get(record["id"], {})
        if located.get("lat") is None:
            unplaced.append(record)
            continue

        properties = {k: record.get(k, "") for k in PUBLISH_FIELDS}
        properties["confidence"] = located.get("confidence", "none")
        properties["matched"] = located.get("matched", "")

        # Baked-in fallback for the live status fetch. The browser overlays
        # whatever the endpoint returns on top of these.
        state = status.get(record["id"], {})
        properties["state"] = state.get("state", "")
        properties["state_updated"] = state.get("updated", "")
        properties["state_by"] = state.get("by", "")
        properties["state_note"] = state.get("note", "")

        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [located["lon"], located["lat"]],
                },
                "properties": properties,
            }
        )

    # A status row whose id matches no record is a sign whose address changed in
    # the source form: record_id() hashes street+suburb, so an edited typo mints
    # a new id and strands the old status. Report it rather than dropping it
    # quietly - the street and suburb are in the sheet so a human can re-key the
    # row, but only if they know to.
    known = {r["id"] for r in records}
    orphans = [
        f"{v.get('street') or '?'}, {v.get('suburb') or '?'} ({k}) = {v.get('state') or 'unset'}"
        for k, v in status.items()
        if k not in known and v.get("state")
    ]

    payload = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "counts": {
            "total": len(records),
            "mapped": len(features),
            "unplaced": len(unplaced),
            "high": sum(1 for r in records if r["priority"] == "high"),
            "up": sum(1 for f in features if f["properties"]["state"] == "up"),
            "down": sum(1 for f in features if f["properties"]["state"] == "down"),
        },
        "geojson": {"type": "FeatureCollection", "features": features},
        "unplaced": [
            {k: r.get(k, "") for k in ("id", "street", "suburb", "priority")}
            for r in unplaced
        ],
    }

    configured = status_config()
    if configured:
        payload["status"] = configured

    write_encrypted(OUT, payload, passphrase)
    size_kb = OUT.stat().st_size / 1024
    print(f"encrypted {len(features)} features -> {OUT.relative_to(ROOT)} ({size_kb:.1f} KB)")
    if unplaced:
        print(f"  {len(unplaced)} unplaced, listed in-page for review")
    print(
        f"  status: {payload['counts']['up']} up / {payload['counts']['down']} down"
        + ("" if configured else "  (endpoint not configured; read-only)")
    )
    if orphans:
        message = (
            f"{len(orphans)} status row(s) match no current record, most likely an "
            f"address edited in the form: {'; '.join(orphans[:10])}"
        )
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::warning title=Orphaned status rows::{message}")
        print(f"WARNING: {message}", file=sys.stderr)


if __name__ == "__main__":
    main()
