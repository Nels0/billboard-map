"""Combine records with geocoded coordinates and publish encrypted map data.

Writes exactly one artefact into site/: a file holding one AES-GCM envelope per
access tier. Nothing readable leaves this script, because site/ is what gets
published to GitHub Pages.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from crypto_util import passphrase_env, write_tiers

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


# The lite tier: the same map, minus anything identifying the host. Dropped
# outright rather than hidden in the page, because a field the lite passphrase
# can decrypt is a field the lite passphrase has published.
CONTACT_FIELDS = ("name", "email", "phone")

# Unscreened free text. It describes the property ("South side of fence
# please") and is what makes the map usable for delivery, so it is kept - but
# the odd phone number and first name turn up in it, so it gets scrubbed.
FREE_TEXT_FIELDS = (
    "property_type",
    "sign_location",
    "mounting",
    "poster_size",
    "is_home",
    "notes",
    "state_note",
)

EMAILISH = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# A run of digits and phone punctuation. The digit count is checked separately,
# so "2 x 1.2m" and "no. 14" survive and "021 555 1234" does not.
PHONEISH = re.compile(r"\+?\d[\d\s().+-]{5,}\d")
REDACTED = "[redacted]"


def scrub(text: str) -> str:
    """Remove phone- and email-shaped substrings from free text."""
    if not text:
        return text
    cleaned = EMAILISH.sub(REDACTED, text)
    cleaned = PHONEISH.sub(
        lambda m: REDACTED if sum(c.isdigit() for c in m.group()) >= 7 else m.group(),
        cleaned,
    )
    return cleaned


def redact(properties: dict) -> dict:
    """A copy of one feature's properties with nothing host-identifying left."""
    lite = dict(properties)
    for field in CONTACT_FIELDS:
        lite[field] = ""
    for field in FREE_TEXT_FIELDS:
        lite[field] = scrub(lite.get(field, ""))
    return lite


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

    # The lite tier is the same payload with the contact fields blanked and the
    # free text scrubbed BEFORE encryption, so its ciphertext simply does not
    # contain them. The status endpoint and token are carried unchanged: the
    # Apps Script only ever reads and writes the two up/down tabs, so a lite
    # holder gains nothing from them that the map itself does not already give.
    tiers = {"full": (payload, passphrase)}
    lite_passphrase = os.environ.get(passphrase_env("lite"), "").strip()
    if lite_passphrase == passphrase:
        sys.exit(
            f"{passphrase_env('lite')} equals MAP_PASSPHRASE; the lite tier would "
            "grant nothing less than the full one."
        )
    if lite_passphrase:
        lite = dict(payload)
        lite["geojson"] = {
            "type": "FeatureCollection",
            "features": [
                {**f, "properties": redact(f["properties"])} for f in features
            ],
        }
        tiers["lite"] = (lite, lite_passphrase)

    write_tiers(OUT, tiers)
    size_kb = OUT.stat().st_size / 1024
    print(
        f"encrypted {len(features)} features -> {OUT.relative_to(ROOT)} "
        f"({size_kb:.1f} KB, tiers: {', '.join(tiers)})"
    )
    if "lite" not in tiers:
        # Not an error: the pipeline predates the lite tier and must keep
        # running without it.
        print(f"  no {passphrase_env('lite')} set; full tier only")
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
