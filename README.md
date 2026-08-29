# Sheet → Map

Turns address data from a Google Sheet into a map, behind a shared passphrase. A
scheduled GitHub Action re-reads the sheet, geocodes any new addresses, and
republishes, so the map stays current without anyone re-exporting anything.

Built for form responses where the address data is messy: free-text fields,
inconsistent spellings, and columns that mean different things on different rows.

## How it fits together

```
Google Sheet ──(service account, read-only)──▶ fetch_sheet.py
                                                    │
                                              normalise.py     messy rows → one record shape
                                                    │
                                              geocode.py       Nominatim, region-bounded, cached
                                                    │
                                              build_site.py    → AES-GCM envelope
                                                    │
                                     GitHub Pages ──▶ site/index.html
                                                       prompts for passphrase,
                                                       decrypts in the browser
```

Nothing readable is ever committed. The repository is public — a requirement for
free GitHub Pages — so the geocode cache is stored as AES-256-GCM ciphertext, and
the map data is never committed at all: it reaches Pages through the uploaded
artifact and is decrypted only in the viewer's browser.

## Normalising the input

Form exports drift. Questions get reworded, a form gets rebuilt, and the export
concatenates responses whose columns no longer line up — so the same column can
hold a postcode on one row and a free-text answer on the next.

`scripts/normalise.py` addresses columns **positionally** rather than by header,
detects which schema a row belongs to, and emits one canonical record shape. It
also folds free-text spelling variants of place names together via an alias map.

Both of those are deployment-specific. See `CLAUDE.md` for this deployment's
column mapping and aliases.

## Geocoding and the review queue

Lookups go to Nominatim, bounded to a geographic viewbox, so a mistyped street
fails to match rather than landing somewhere plausible in another city. Each
result is graded:

- **exact** — the geocoder returned the matching house number
- **street** — the street matched but not the number
- **suburb** — only a suburb centroid resolved; the pin is indicative
- **none** — nothing matched; listed in-page as "could not be placed"

Several fallbacks run before giving up: unit/flat notation (`2/175 Example
Street`), free-text prefixes (`Top Flat, 12a Example Road`), and a street-type
swap for when someone writes Street where the road is really a Road. The
type-swap is deliberately capped at `street` confidence so a human always
confirms the substitution.

Match quality is a caveat on a pin, not a headline: pins are coloured by
priority, and anything below `exact` carries a note at the bottom of its card
showing what the geocoder actually matched. The **Only ones needing review**
filter isolates them for a cleanup pass. Fix those at the source and the next run
picks them up. Results are cached by a hash of the address, so a scheduled run
only geocodes genuinely new entries.

## Privacy

This carries personal information — names, phone numbers, email addresses and
home addresses. The design follows from that:

- `build_site.py` publishes an **allowlist** of fields, so a new column in the
  export cannot reach the map without someone adding it here on purpose.
- Contact details **are** published, because the delivery team needs to reach
  people. The passphrase is therefore the only thing between a public URL and
  someone's name, phone and email — not merely their street address.
- The page vendors Leaflet locally instead of loading it from a CDN. A page that
  prompts for a passphrase should not run third-party script that could capture
  it.
- Free-text fields are rendered through an HTML-escaping helper, since they
  arrive from a public form.

**Limits of the passphrase model.** One shared secret protects everyone. There is
no per-person revocation — removing someone's access means changing the
passphrase and telling everybody the new one, and it cannot stop an authorised
viewer copying what they see. Because the ciphertext is public and archived, a
passphrase that leaks later retroactively decrypts everything ever published.

That is appropriate for a small trusted team, not for wide distribution. Past a
handful of people, move to Cloudflare Pages with Cloudflare Access (free for up
to 50 users), which gives per-person allowlisting and revocation. Only the
hosting layer changes.

## Supply chain

The scheduled job reads a service account key and a passphrase, so anything
executing inside it could read both. Several pins keep that set closed:

- **Actions are pinned to commit SHAs**, not tags, with the version in a trailing
  comment. A tag can be repointed at new code by whoever controls the action's
  repository — the mechanism behind the `tj-actions/changed-files` compromise. A
  SHA cannot be repointed.
- **Dependencies install from `requirements.lock` with `--require-hashes`**,
  pinning every package including transitive ones and refusing any artifact whose
  hash is not listed.
- **Secrets are attached to individual steps**, not the job, so `checkout`,
  `setup-python` and the Pages actions never see the key or the passphrase.

`requirements.txt` holds the loose direct dependencies; it is the input to the
lockfile, not what CI installs. Regenerate after changing it:

```bash
uv pip compile requirements.txt --generate-hashes \
  --python-version 3.12 --output-file requirements.lock
```

Pinning freezes versions, so Dependabot (`.github/dependabot.yml`) proposes
weekly updates for both ecosystems rather than letting the pins go stale.

One thing this does **not** cover: the job runs with `contents: write` and pushes
to `main`, so a compromise could alter the published page as well as read
whatever that step could see.

## Setup

1. **Create the repo and push.** It must be public for GitHub Pages on a free
   account. Only the encrypted cache and static assets are committed.
2. **Service account** — in Google Cloud, create a project, **enable the Sheets
   API on that project**, create a service account, and download its JSON key.
   Share the sheet with the service account's email address as a **Viewer**; it
   is its own principal, so sharing with your own account does not cover it. If
   you only have view access and cannot share it onward, make your own sheet that
   pulls the data in with `=IMPORTRANGE(...)` and point `SHEET_ID` at yours.
3. **Repository secrets** (Settings → Secrets and variables → Actions):
   - `MAP_PASSPHRASE` — the passphrase given to viewers
   - `SHEET_ID` — the id from the sheet URL, between `/d/` and `/edit`
   - `GOOGLE_SERVICE_ACCOUNT_JSON` — the whole key file contents
4. **Repository variable** `SHEET_TAB` — the tab holding the responses. Without
   it the API reads whichever tab is first, which may not be the right one; the
   fetcher falls back to detecting the tab by its header row.
5. **Settings → Pages → Source: GitHub Actions.**
6. Run the **Update map** workflow once to seed the cache.

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

`build/`, `data/` and `site/data.enc.json` are gitignored; they are the only
places plaintext or locally-keyed data exists. Use a throwaway passphrase
locally — `site/data.enc.json` is deliberately untracked so a local build can
never be committed over the real one.
