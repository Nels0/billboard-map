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


def main() -> None:
    passphrase = os.environ.get("MAP_PASSPHRASE")
    if not passphrase:
        sys.exit("MAP_PASSPHRASE is not set; refusing to build.")

    records = json.loads(RECORDS.read_text(encoding="utf-8"))
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}

    features, unplaced = [], []
    for record in records:
        located = cache.get(record["id"], {})
        if located.get("lat") is None:
            unplaced.append(record)
            continue

        properties = {k: record.get(k, "") for k in PUBLISH_FIELDS}
        properties["confidence"] = located.get("confidence", "none")
        properties["matched"] = located.get("matched", "")
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

    payload = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "counts": {
            "total": len(records),
            "mapped": len(features),
            "unplaced": len(unplaced),
            "high": sum(1 for r in records if r["priority"] == "high"),
        },
        "geojson": {"type": "FeatureCollection", "features": features},
        "unplaced": [
            {k: r.get(k, "") for k in ("id", "street", "suburb", "priority")}
            for r in unplaced
        ],
    }

    write_encrypted(OUT, payload, passphrase)
    size_kb = OUT.stat().st_size / 1024
    print(f"encrypted {len(features)} features -> {OUT.relative_to(ROOT)} ({size_kb:.1f} KB)")
    if unplaced:
        print(f"  {len(unplaced)} unplaced, listed in-page for review")


if __name__ == "__main__":
    main()
