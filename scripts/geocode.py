"""Geocode canonical records against OpenStreetMap via Nominatim.

Every request is bounded to a Wellington viewbox, so a sloppy street name can
fail to match but cannot silently land in another city. Results are cached by
record id, so a scheduled run only pays for addresses it has not seen before.

Each result carries a confidence:
  exact       Nominatim returned the matching house number
  street      the street matched but not the number
  suburb      only the suburb centroid resolved; the pin is indicative only
  none        nothing matched inside the viewbox
Anything below `exact` is listed for human review rather than quietly mapped.
"""

import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RECORDS = ROOT / "build" / "records.plain.json"
CACHE = ROOT / "build" / "geocode-cache.plain.json"

ENDPOINT = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "billboard-map/1.0 (github.com/Nels0/billboard-map)"

# Wellington city and the eastern/southern bays. Bounded searches outside this
# box return nothing rather than a plausible-looking match elsewhere in NZ.
VIEWBOX = "174.70,-41.37,174.90,-41.24"

MIN_INTERVAL = 1.1  # Nominatim usage policy: at most 1 request per second.
_last_call = 0.0

FLAT = re.compile(r"(\d+[A-Za-z]?)\s*/\s*(\d+[A-Za-z]?)")
FIRST_NUMBER = re.compile(r"\b(\d+[A-Za-z]?)\b")
UNIT_PREFIX = re.compile(r"^\s*[\w-]+\s*/\s*")
FROM_FIRST_DIGIT = re.compile(r"\d.*$")


def wanted_numbers(street: str) -> set[str]:
    """House numbers we would accept as an exact hit.

    OSM records NZ flats inconsistently: for "1/68 Example Street" the house
    number may be stored as "1/68" or as "68", so both spellings count.
    """
    flat = FLAT.search(street)
    if flat:
        return {f"{flat.group(1)}/{flat.group(2)}".lower(), flat.group(2).lower()}
    first = FIRST_NUMBER.search(street)
    return {first.group(1).lower()} if first else set()


def street_variants(street: str) -> list[str]:
    """Progressively simpler spellings to try when the raw string fails.

    Handles free-text prefixes ("Top Flat, 12a Example Road") and unit numbers
    ("4a/20 Example Street"), both of which Nominatim parses poorly.
    """
    variants: list[str] = []

    def add(candidate: str) -> None:
        candidate = candidate.strip(" ,")
        if candidate and candidate not in variants:
            variants.append(candidate)

    add(street)
    trimmed = FROM_FIRST_DIGIT.search(street)
    if trimmed:
        add(trimmed.group(0))
    add(UNIT_PREFIX.sub("", street))
    if trimmed:
        add(UNIT_PREFIX.sub("", trimmed.group(0)))
    return variants


def request(params: dict) -> list[dict]:
    global _last_call
    wait = MIN_INTERVAL - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)

    query = urllib.parse.urlencode(
        {
            "format": "jsonv2",
            "countrycodes": "nz",
            "viewbox": VIEWBOX,
            "bounded": "1",
            "addressdetails": "1",
            "limit": "1",
            **params,
        }
    )
    req = urllib.request.Request(
        f"{ENDPOINT}?{query}", headers={"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - a failed lookup is data, not a crash
        print(f"    ! request failed: {exc}", file=sys.stderr)
        return []
    finally:
        _last_call = time.monotonic()


RANK = {"exact": 3, "street": 2, "suburb": 1, "none": 0}

# Respondents sometimes give the wrong street type, writing Street where the
# road is really a Road. Swapping it is a last resort and never scores better
# than "street", so a human always confirms the substitution was right.
STREET_TYPES = (
    "Street", "Road", "Avenue", "Drive", "Crescent",
    "Terrace", "Place", "Lane", "Parade", "Grove", "Way",
)
TRAILING_TYPE = re.compile(
    r"\b(" + "|".join(STREET_TYPES) + r"|St|Rd|Ave|Av|Dr|Cres|Tce|Pl|Ln|Pde|Gr)\b\s*$",
    re.IGNORECASE,
)


def type_swapped(street: str) -> list[str]:
    match = TRAILING_TYPE.search(street)
    if not match:
        return []
    stem = street[: match.start()].rstrip()
    current = match.group(1).lower()
    return [
        f"{stem} {t}"
        for t in STREET_TYPES
        if t.lower() != current and not t.lower().startswith(current[:2])
    ]


def geocode(record: dict) -> dict:
    street, suburb = record["street"], record["suburb"]
    acceptable = wanted_numbers(street)

    best = {"lat": None, "lon": None, "confidence": "none", "matched": "", "via": ""}

    for variant in street_variants(street):
        queries = [(f"{variant}, {suburb}, Wellington, New Zealand", "street+suburb")]
        if suburb:
            queries.append((f"{variant}, Wellington, New Zealand", "street"))

        for query, label in queries:
            results = request({"q": query})
            if not results:
                continue
            hit = results[0]
            address = hit.get("address", {})
            got_number = (address.get("house_number") or "").lower()

            if acceptable and got_number in acceptable:
                confidence = "exact"
            elif address.get("road"):
                confidence = "street"
            else:
                confidence = "suburb"

            if RANK[confidence] > RANK[best["confidence"]]:
                best = {
                    "lat": float(hit["lat"]),
                    "lon": float(hit["lon"]),
                    "confidence": confidence,
                    "matched": hit.get("display_name", ""),
                    "house_number": got_number,
                    "via": f"{label}: {variant}",
                }
            if confidence == "exact":
                return best

    # Nothing usable yet: the street type itself may be wrong.
    if RANK[best["confidence"]] < RANK["street"]:
        trimmed = FROM_FIRST_DIGIT.search(street)
        base = UNIT_PREFIX.sub("", trimmed.group(0) if trimmed else street).strip(" ,")
        for candidate in type_swapped(base):
            results = request({"q": f"{candidate}, {suburb}, Wellington, New Zealand"})
            if not results:
                continue
            hit = results[0]
            got_number = (hit.get("address", {}).get("house_number") or "").lower()
            if acceptable and got_number in acceptable:
                return {
                    "lat": float(hit["lat"]),
                    "lon": float(hit["lon"]),
                    # Deliberately not "exact": the street type was changed.
                    "confidence": "street",
                    "matched": hit.get("display_name", ""),
                    "house_number": got_number,
                    "via": f"type-swap: {candidate}",
                }

    if best["confidence"] != "none":
        return best

    if suburb:
        results = request({"q": f"{suburb}, Wellington, New Zealand"})
        if results:
            hit = results[0]
            return {
                "lat": float(hit["lat"]),
                "lon": float(hit["lon"]),
                "confidence": "suburb",
                "matched": hit.get("display_name", ""),
                "via": "suburb centroid",
            }

    return best


def main() -> None:
    records = json.loads(RECORDS.read_text(encoding="utf-8"))
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}

    pending = [r for r in records if r["id"] not in cache]
    print(f"{len(records)} records, {len(cache)} cached, {len(pending)} to geocode")
    if pending:
        print(f"  ~{len(pending) * MIN_INTERVAL / 60:.1f} min at 1 req/sec\n")

    for index, record in enumerate(pending, 1):
        result = geocode(record)
        cache[record["id"]] = result
        marker = {"exact": "  ", "street": " ~", "suburb": " ?", "none": " X"}[
            result["confidence"]
        ]
        print(
            f"{marker} [{index:3d}/{len(pending)}] {record['street']}, "
            f"{record['suburb']} -> {result['confidence']}"
        )
        CACHE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), "utf-8")

    tally: dict[str, int] = {}
    for record in records:
        level = cache.get(record["id"], {}).get("confidence", "none")
        tally[level] = tally.get(level, 0) + 1

    print("\nconfidence:")
    for level in ("exact", "street", "suburb", "none"):
        if tally.get(level):
            print(f"  {level:8s} {tally[level]}")

    review = [
        r
        for r in records
        if cache.get(r["id"], {}).get("confidence") in {"suburb", "none"}
    ]
    if review:
        print(f"\n{len(review)} need review:")
        for record in review:
            print(f"  - {record['street']}, {record['suburb']}")


if __name__ == "__main__":
    main()
