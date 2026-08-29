# WLB26 Billboard Map

Maps billboard/sign requests from the online form onto a Wellington map, behind a
shared passphrase. A scheduled GitHub Action re-reads the sheet, geocodes any new
addresses, and republishes — so the map keeps itself current without anyone
re-exporting anything.

## How it fits together

```
Google Sheet ──(service account, read-only)──▶ fetch_sheet.py
                                                    │
                                              normalise.py     two form schemas → one record shape
                                                    │
                                              geocode.py       Nominatim, Wellington-bounded, cached
                                                    │
                                              build_site.py    → AES-GCM envelope
                                                    │
                                     GitHub Pages ──▶ site/index.html
                                                       prompts for passphrase,
                                                       decrypts in the browser
```

Nothing readable is ever committed. The repository is public (a requirement for
free GitHub Pages), so both the map data and the geocode cache are stored as
AES-256-GCM ciphertext and only ever decrypted in the browser or in CI.

## The data quirk this exists to solve

The export concatenates responses from **two different versions of the form**.
Rows with an Entry ID came from the original; rows without came from a later one,
and their answers landed under the original's headers. So the same column means
different things depending on the row:

| Column header | Rows with Entry ID (46) | Rows without (57) |
|---|---|---|
| `-Street Address`, `-Street Address Line 2`, `PRIORITY` | as labelled | **as labelled** |
| `-Region` | Wellington | property type |
| `-Postal / Zip Code` | 6021, 6023 … | fence/mounting description |
| `-Country` | New Zealand | poster size |
| `What electorate are you in?` | Wellington Bays | permission confirmed |
| `Where will the sign go?-On a fence` | yes/no | electorate code (`WLB`) |

Street, suburb and priority are the only fields that survive both, which is why
geocoding keys off those three alone. `normalise.py` detects the schema per row
and remaps the rest, so popups show correctly labelled information either way.

It also folds free-text suburb spellings together (`Mount cook`, `Mt Cook`,
`Mt cook` → `Mt Cook`; `strathmore` → `Strathmore Park`), taking 25 spellings
down to 18 real suburbs.

## Geocoding and the review queue

Every lookup is bounded to a Wellington viewbox, so a mistyped street fails to
match rather than landing somewhere plausible in another city. Each result is
graded:

- **exact** — Nominatim returned the matching house number
- **street** — the street matched but not the number
- **suburb** — only a suburb centroid resolved; the pin is indicative
- **none** — nothing matched; listed in-page as "could not be placed"

Anything below `exact` is drawn with a dashed amber ring and can be isolated with
the **Only ones needing review** filter. Fix those in the sheet and the next run
picks them up. Results are cached by a hash of street+suburb, so a scheduled run
only geocodes genuinely new addresses.

## Privacy

The sheet holds home addresses of people who volunteered to host political
signage. Two deliberate choices follow from that:

- `build_site.py` publishes an **allowlist** of fields. Name, email and phone are
  never emitted, even if a future export repopulates those columns.
- The page vendors Leaflet locally instead of loading it from a CDN. A page that
  prompts for a passphrase should not run third-party script that could capture
  it.

**Limits of the passphrase model.** One shared secret protects everyone. There is
no per-person revocation — removing someone's access means changing the
passphrase and telling everybody the new one. It also can't stop an authorised
viewer from copying what they see. It is appropriate for a small trusted team,
not for wide distribution. If the recipient list grows past a handful of people,
move to Cloudflare Pages with Cloudflare Access (free for up to 50 users), which
gives per-person allowlisting and revocation; only the hosting layer changes.

## One-time setup

1. **Create the repo and push.** It must be public for GitHub Pages on a free
   account. Only ciphertext and static assets are committed.
2. **Service account** — in Google Cloud, create a project, enable the Sheets
   API, create a service account, download its JSON key. Share the sheet with the
   service account's email address as a **Viewer**.
   If you only have view access to the sheet and cannot share it onward, make
   your own sheet that pulls it in with `=IMPORTRANGE(...)` and point `SHEET_ID`
   at yours instead.
3. **Repository secrets** (Settings → Secrets and variables → Actions):
   - `MAP_PASSPHRASE` — the passphrase you give viewers
   - `SHEET_ID` — the id from the sheet URL
   - `GOOGLE_SERVICE_ACCOUNT_JSON` — the whole key file contents
4. **Settings → Pages → Source: GitHub Actions.**
5. Run the **Update map** workflow manually once to seed `cache.enc.json`.

Changing `MAP_PASSPHRASE` later requires deleting `cache.enc.json` in the same
commit, since the old cache can no longer be decrypted.

## Running locally

```bash
pip install -r requirements.txt
export MAP_PASSPHRASE='…'

python scripts/normalise.py            # data/responses.csv → build/records.plain.json
python scripts/geocode.py              # adds build/geocode-cache.plain.json
python scripts/build_site.py           # → site/data.enc.json
python -m http.server -d site 8000     # open http://localhost:8000
```

`build/` and `data/` are gitignored; they are the only places plaintext exists.
