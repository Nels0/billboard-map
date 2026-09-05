.PHONY: build serve fetch clean check test

# Full local rebuild from the CSV already in data/.
build:
	python3 scripts/normalise.py
	python3 scripts/geocode.py
	python3 scripts/build_site.py

# Pull a fresh copy of the sheet first (needs SHEET_ID + service account).
# fetch_status.py is a no-op unless STATUS_SHEET_ID or SHEET_ID is set.
fetch:
	python3 scripts/fetch_sheet.py
	python3 scripts/fetch_status.py

serve: build
	@echo "http://localhost:8000"
	python3 -m http.server -d site 8000

# Same guard the workflow runs: nothing readable may sit in site/.
check:
	@python3 -c "import json,sys; d=json.load(open('site/data.enc.json')); \
	t=d['tiers'] if 'tiers' in d else {'full': d}; \
	bad=[(n,{'v','salt','iv','ct'}-set(e)) for n,e in t.items() if {'v','salt','iv','ct'}-set(e)]; \
	sys.exit(f'not an envelope: {bad}') if bad else print('site/data.enc.json encrypted OK (' + ', '.join(t) + ')')"
	@git status --porcelain site/ | grep -vE 'site/(data\.enc\.json|index\.html|vendor/)' \
	  && { echo 'unexpected file under site/'; exit 1; } || echo 'site/ contents OK'

# The page's live-status refresh loop, sliced out of site/index.html and run
# against stubs. Node only; no dependencies, no build step.
test:
	node --test tests/*.test.js

clean:
	rm -rf build
