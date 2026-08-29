"""Turn the raw form export into canonical sign-request records.

The export concatenates two different form versions. Rows carrying an Entry ID
come from the original form and their headers mean what they say. Rows without
one come from a later form whose answers landed under the original headers, so
`-Region` holds a property type, `-Postal / Zip Code` holds mounting notes, and
so on. Street, suburb and PRIORITY are the only fields that mean the same thing
in both, which is why geocoding keys off those alone.

Output columns are an allowlist: name, email and phone are never emitted even if
a future export repopulates them.
"""

import csv
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Header prefixes are long and duplicated; address them positionally instead.
COL_ENTRY_ID = 0
# Contact columns sit ahead of the address block, so they hold the same thing in
# both form versions even though the columns after PRIORITY diverge.
COL_FIRST = 2
COL_LAST = 3
COL_EMAIL = 4
COL_PHONE = 5
COL_STREET = 7
COL_SUBURB = 8
COL_PRIORITY = 9
COL_C10 = 10  # A: region          B: property type
COL_C11 = 11  # A: postcode        B: mounting / where the sign goes
COL_C12 = 12  # A: country         B: poster size / free text
COL_C13 = 13  # A: electorate      B: permission confirmed
COL_C14 = 14  # A: is this home    B: is this home / access note
COL_C15 = 15  # A: permission      B: delivery & access instructions
COL_C16 = 16  # A: sign on fence   B: electorate code
COL_ON_HOUSE = 17
COL_IN_WINDOW = 18
COL_FRONT_YARD = 19
COL_OTHER_PLACE = 20
COL_AFFIX_PREF = 21
COL_NOTES = 22

WS = re.compile(r"\s+")

# The form takes free text, so the same suburb arrives in several spellings.
# Keys are lowercased; anything unlisted just gets title-cased.
SUBURB_ALIASES = {
    "mount cook": "Mt Cook",
    "mt cook": "Mt Cook",
    "strathmore": "Strathmore Park",
    "strathmore park": "Strathmore Park",
    "karaka bays": "Karaka Bays",
    "owhiro bay": "Ōwhiro Bay",
}


def canonical_suburb(value: str) -> str:
    cleaned = clean(value)
    if not cleaned:
        return ""
    return SUBURB_ALIASES.get(cleaned.lower(), cleaned.title())


def clean(value: str) -> str:
    """Trim, collapse internal whitespace, drop trailing separators."""
    if not value:
        return ""
    return WS.sub(" ", value).strip().strip(",;").strip()


def yes(value: str) -> bool:
    return clean(value).lower() in {"yes", "y", "true"}


def record_id(street: str, suburb: str) -> str:
    """Stable across re-exports and row reordering, unlike a row index."""
    key = f"{street.lower()}|{suburb.lower()}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def sign_locations(row: list[str]) -> list[str]:
    """Group A records where the sign goes as four yes/no checkbox columns."""
    labels = [
        (COL_C16, "fence"),
        (COL_ON_HOUSE, "house"),
        (COL_IN_WINDOW, "window"),
        (COL_FRONT_YARD, "front yard"),
        (COL_OTHER_PLACE, "other"),
    ]
    return [label for idx, label in labels if yes(get(row, idx))]


def get(row: list[str], idx: int) -> str:
    return row[idx] if idx < len(row) else ""


def normalise(path: Path) -> tuple[list[dict], dict]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        next(reader)  # discard header; positions are what matter
        raw = [r for r in reader if any(clean(c) for c in r)]

    records, seen, stats = [], {}, {"A": 0, "B": 0, "dropped": 0, "duplicates": 0}

    for row in raw:
        street = clean(get(row, COL_STREET))
        suburb = canonical_suburb(get(row, COL_SUBURB))
        if not street:
            stats["dropped"] += 1
            continue

        schema = "A" if clean(get(row, COL_ENTRY_ID)) else "B"
        stats[schema] += 1
        rid = record_id(street, suburb)

        rec = {
            "id": rid,
            "schema": schema,
            "street": street,
            "suburb": suburb,
            "priority": clean(get(row, COL_PRIORITY)).lower() or "low",
            "name": " ".join(
                p for p in (clean(get(row, COL_FIRST)), clean(get(row, COL_LAST))) if p
            ),
            "email": clean(get(row, COL_EMAIL)),
            "phone": clean(get(row, COL_PHONE)),
        }

        if schema == "A":
            rec |= {
                "postcode": clean(get(row, COL_C11)),
                "property_type": "",
                "sign_location": ", ".join(sign_locations(row)),
                "mounting": clean(get(row, COL_AFFIX_PREF)),
                "poster_size": "",
                "permission": "yes" if yes(get(row, COL_C15)) else "",
                "is_home": clean(get(row, COL_C14)),
                "notes": clean(get(row, COL_NOTES)),
            }
        else:
            rec |= {
                "postcode": "",
                "property_type": clean(get(row, COL_C10)),
                "sign_location": clean(get(row, COL_C11)),
                "mounting": clean(get(row, COL_C15)),
                "poster_size": clean(get(row, COL_C12)),
                "permission": "yes" if yes(get(row, COL_C13)) else "",
                "is_home": clean(get(row, COL_C14)),
                "notes": clean(get(row, COL_NOTES)),
            }

        # Same address submitted twice: keep the higher priority and merge notes.
        if rid in seen:
            stats["duplicates"] += 1
            prev = seen[rid]
            if rec["priority"] == "high":
                prev["priority"] = "high"
            # One submission may carry contact details the other left blank.
            for field in ("name", "email", "phone"):
                if not prev[field] and rec[field]:
                    prev[field] = rec[field]
            for field in ("notes", "mounting", "sign_location"):
                if rec[field] and rec[field] not in prev[field]:
                    prev[field] = f"{prev[field]} / {rec[field]}".strip(" /")
            continue

        seen[rid] = rec
        records.append(rec)

    return records, stats


def main() -> None:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "responses.csv"
    out = ROOT / "build" / "records.plain.json"
    out.parent.mkdir(exist_ok=True)

    records, stats = normalise(src)
    out.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")

    suburbs = sorted({r["suburb"] for r in records if r["suburb"]})
    high = sum(1 for r in records if r["priority"] == "high")
    print(f"{len(records)} records -> {out.relative_to(ROOT)}")
    print(f"  schema A (original form): {stats['A']}")
    print(f"  schema B (later form):    {stats['B']}")
    print(f"  duplicate addresses merged: {stats['duplicates']}")
    print(f"  rows dropped (no street):   {stats['dropped']}")
    print(f"  priority: {high} high / {len(records) - high} low")
    print(f"  {len(suburbs)} suburbs: {', '.join(suburbs)}")


if __name__ == "__main__":
    main()
