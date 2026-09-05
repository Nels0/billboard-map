# Sheet → Map

Turns address data from a Google Sheet into a map, behind a shared passphrase. A
scheduled GitHub Action re-reads the sheet, geocodes any new addresses, and
republishes, so the map stays current without anyone re-exporting anything.

There are two passphrases. The second is optional and opens a copy of the same
map with the contact details removed *before* encryption — see
[Two access tiers](#two-access-tiers).

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
                                                    │             (one per tier)
                                                    │
                                     GitHub Pages ──▶ site/index.html
                                                       prompts for passphrase,
                                                       decrypts in the browser
                                                            ▲
                                                            │ live, read/write
                                                            ▼
status tab ◀──▶ Apps Script web app ──────────────── up / down state only
```

Addresses travel the slow path on the left: a scheduled rebuild that geocodes,
encrypts and republishes. Mutable state travels the fast path on the right,
straight between the browser and a small endpoint, and carries nothing but a
record id and a status. The two are joined in the browser at render time.

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

## Tracking status

Whether a sign is actually up is the one field that changes hourly and that the
people in the field, not the form, are the authority on. It therefore does not
travel through the rebuild at all.

A second spreadsheet tab holds one row per address — a record id, the address in
plain text, a state, and who set it when. An Apps Script web app bound to that
spreadsheet reads and writes it; the map calls that endpoint directly. A separate
append-only tab logs every write, so "who marked this up, and when" has an answer
after a sign goes missing.

Three properties are worth keeping if this is adapted:

- **The endpoint URL and its token live inside the encrypted payload**, not in
  `index.html`. A deployment URL plus token is a bearer capability to write, so
  it belongs behind the same passphrase as the addresses rather than being served
  in the clear to every visitor.
- **The build bakes in a snapshot** of the status tab as a fallback. If the
  endpoint is unreachable the map shows last-known state and says so, rather than
  blanking every pin — a pin with no status reads as "nobody has been here yet",
  which is a lie that sends someone to the same address twice.
- **Writes are optimistic and reversible.** The state applies on tap and reverts
  with a visible message if the endpoint rejects it, because a field team on
  mobile data should not wait on a round trip per tap.

The endpoint keys on a hash of street and suburb, the same id the rest of the
pipeline uses. Correcting an address in the source form therefore mints a new id
and strands the old status row, so `build_site.py` reports rows matching no
current record instead of dropping them quietly; the address is stored in plain
text alongside the id precisely so a human can re-key one.

Two constraints come from Apps Script itself, and both are easy to trip over.
Web apps do not answer CORS preflight requests, so calls from the page must stay
"simple": no custom headers, which is why the token travels in the query string
and the request body, and why the POST is sent as `text/plain` despite carrying
JSON. And re-deploying the script issues a **new URL**, so the endpoint secret
has to be updated whenever the script changes.

Everything degrades: with no endpoint configured the map still shows whatever
status the last build baked in, as a read-only field.

## Two access tiers

Some people need the map and the delivery notes but have no reason to hold the
contact list. `build_site.py` therefore builds the feature collection twice and
encrypts each copy under its own passphrase:

| Tier | Passphrase | Sees |
|---|---|---|
| `full` | `MAP_PASSPHRASE` | everything, contact details included |
| `lite` | `MAP_PASSPHRASE_LITE` | the map, addresses, delivery notes and the up/down controls — no names, emails or phone numbers |

`site/data.enc.json` holds one envelope per tier:

```json
{ "v": 2, "tiers": { "full": {"salt": "…", "iv": "…", "ct": "…"},
                     "lite": {"salt": "…", "iv": "…", "ct": "…"} } }
```

The page tries each tier in turn and keeps whichever one the passphrase opened,
so a viewer types one passphrase and never chooses a tier. Points worth keeping
in mind if you change this:

- **The redaction happens in Python, before encryption.** The lite ciphertext
  simply does not contain the contact fields. Shipping the full payload and
  hiding fields in the page would leave the phone numbers sitting in ciphertext
  that the lite passphrase opens.
- **Free text is scrubbed, not dropped.** The notes and mounting fields describe
  the property rather than the person and are what make the map usable for
  delivery, so they are kept — with phone- and email-shaped substrings replaced
  by `[redacted]`. That is a pattern match, not a guarantee: a bare first name
  written into a note survives it.
- **One file, not two**, so the "nothing readable under `site/`" guard keeps its
  allowlist and only has to check each envelope in turn.
- **Unlocking costs one PBKDF2 run per tier tried**, so a wrong passphrase now
  does roughly twice the work on an old phone. There is no safe shortcut: a
  cheap per-tier verifier would hand an attacker a fast oracle to brute-force
  instead of the 600k-iteration stretch.
- **Both tiers carry the same status endpoint and token.** The Apps Script only
  ever reads and writes the two up/down tabs, so this hands lite holders nothing
  they do not already have through the map. It does mean someone who kept the
  token still has it after the lite passphrase is rotated; closing that means
  rotating `STATUS_TOKEN` too, which logs out every viewer until they reload.
- **`MAP_PASSPHRASE_LITE` is optional.** Unset, the build emits the `full` tier
  alone and the file keeps the same shape, so the scheduled run never breaks for
  want of a secret. The page also still opens a pre-v2 file that is a single
  bare envelope.

Unlike the full passphrase, the lite one is cheap to rotate: change the secret,
re-run the workflow, and the old one stops decrypting. `MAP_PASSPHRASE` and the
geocode cache are untouched, so no `cache.enc.json` deletion is needed.

## Privacy

This carries personal information — names, phone numbers, email addresses and
home addresses. The design follows from that:

- `build_site.py` publishes an **allowlist** of fields, so a new column in the
  export cannot reach the map without someone adding it here on purpose.
- Contact details **are** published to the full tier, because the delivery team
  needs to reach people. The full passphrase is therefore the only thing between
  a public URL and someone's name, phone and email — not merely their street
  address. The lite tier exists so that not everyone needs that passphrase.
- The page vendors Leaflet locally instead of loading it from a CDN. A page that
  prompts for a passphrase should not run third-party script that could capture
  it.
- Free-text fields are rendered through an HTML-escaping helper, since they
  arrive from a public form.

- Status tracking makes the passphrase a **write** credential as well as a read
  one: anyone who can open the map can mark a sign up or down. That matches the
  trust model — they can already see every address and phone number — and the
  realistic failure is vandalism rather than disclosure, which the append-only
  log makes visible and reversible. The endpoint token can also be rotated
  independently of the passphrase.

**Limits of the passphrase model.** A shared secret protects everyone holding
it. There is no per-person revocation — removing someone's access means changing
the passphrase and telling everybody the new one, and it cannot stop an
authorised viewer copying what they see. The lite tier narrows the blast radius
of the passphrase most people hold, and is far cheaper to rotate, but it is the
same model. Because the ciphertext is public and archived, a
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
   - `MAP_PASSPHRASE` — the passphrase given to viewers who need contact details
   - `MAP_PASSPHRASE_LITE` — optional second passphrase; same map, contact
     details removed before encryption. Must differ from `MAP_PASSPHRASE`; the
     build fails loudly if it does not. Leave it unset to publish one tier only.
   - `SHEET_ID` — the id from the sheet URL, between `/d/` and `/edit`
   - `GOOGLE_SERVICE_ACCOUNT_JSON` — the whole key file contents
4. **Repository variable** `SHEET_TAB` — the tab holding the responses. Without
   it the API reads whichever tab is first, which may not be the right one; the
   fetcher falls back to detecting the tab by its header row.
5. **Settings → Pages → Source: GitHub Actions.**
6. Run the **Update map** workflow once to seed the cache.

**Status tracking is optional** and can be added later; without it the map builds
and runs exactly as before.

7. Add two tabs — `status` and `status_log` — with the header rows named in
   `scripts/apps_script/Code.gs`. They can live in the response spreadsheet or
   in one of their own; a separate spreadsheet is the safer default, because a
   container-bound script can then be authorised under the `spreadsheets`
   `currentonly` scope and cannot reach the sheet holding contact details.
8. Extensions → Apps Script, paste `scripts/apps_script/Code.gs`, and set a
   Script Property `WRITE_TOKEN` to a long random string. It is deliberately not
   in the file, which is committed to a public repository.
9. Deploy → New deployment → Web app, executing as you, accessible to anyone
   with the link. Copy the `/exec` URL.
10. Add secrets `STATUS_ENDPOINT` (that URL) and `STATUS_TOKEN` (the property),
    plus `STATUS_SHEET_ID` if the tabs are not in the response spreadsheet.

Changing `MAP_PASSPHRASE` later requires deleting `cache.enc.json` in the same
commit, since the old cache can no longer be decrypted.

## Running locally

```bash
pip install -r requirements.txt
export MAP_PASSPHRASE='…'
export MAP_PASSPHRASE_LITE='…'                # optional second tier

python scripts/normalise.py            # data/responses.csv → build/records.plain.json
python scripts/geocode.py              # adds build/geocode-cache.plain.json
python scripts/fetch_status.py         # optional; → build/status.plain.json
python scripts/build_site.py           # → site/data.enc.json
python -m http.server -d site 8000     # open http://localhost:8000
```

`fetch_status.py` writes an empty snapshot and exits cleanly when no status
sheet is configured, and warns rather than failing when one is configured but
unreachable — a missing fallback must never break the map build. Set
`STATUS_ENDPOINT` and `STATUS_TOKEN` before `build_site.py` to exercise the live
path locally.

Read a tier back to check what it actually contains:

```bash
MAP_PASSPHRASE_LITE='…' python scripts/crypto_util.py \
  decrypt site/data.enc.json /tmp/lite.json lite
grep -c '@' /tmp/lite.json                    # expect 0
```

`build/`, `data/` and `site/data.enc.json` are gitignored; they are the only
places plaintext or locally-keyed data exists. Use a throwaway passphrase
locally — `site/data.enc.json` is deliberately untracked so a local build can
never be committed over the real one.
