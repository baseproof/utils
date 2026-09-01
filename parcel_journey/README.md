# Parcel Journey Capture — Jefferson County (eringcapture.jccal.org)

Browser-use automation driven by Gemini (Google GenAI SDK, Vertex AI mode),
split into **three sequential agent phases sharing ONE browser session**
(`Browser(keep_alive=True)` reused by each `Agent`):

- **Phase 1 — Search & open**: portal → Real Property Search → select
  'Parcel #' + year → type parcel (dot stripped; the site's input mask
  auto-inserts the period, typing it manually doubles it to `..`) →
  click exact-match autocomplete suggestion (or search button) →
  verify result row character-by-character → View Details.
- **Phase 2 — Verify & expand**: first verifies it is on the correct detail
  page ('Detail for Parcel: <parcel>' heading, Owner label, Valuation Summary
  section, domain check; reports 'VERIFICATION FAILED: ...' and stops if not),
  then expands 'View Historical Tax Payments' and 'View Instruments'
  (idempotent — skips if labels already say 'Hide ...').
- **Phase 3 — Extract** (only runs if Phase 2 reported VERIFIED): scrolls the
  whole page and returns it as the `ParcelDetail` Pydantic model via
  `output_model_schema`.

Outputs: `utils/journey_capture.yaml` (per-phase status + traces) and
`utils/parcel_detail.json` (structured data).

## Files

- `journey_capture_genai.py` — main script (3-phase)
- `parcel_models.py` — Pydantic models. **Vertex-safe**: no `Optional`/`anyOf`
  (Vertex structured output rejects anyOf-null unions); missing values are ''.
- `README.md` — this file

## Setup

Requires Python **3.13** (3.14 beta breaks pydantic; browser-use needs >=3.11).

```bash
uv init                       # if no pyproject.toml yet
uv python pin 3.13            # then set requires-python = ">=3.13,<3.14"
uv add browser-use google-genai google-auth pyyaml pydantic
```

No Playwright needed — modern browser-use manages Chromium itself.

## Credentials

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json  # or ADC_JSON_PATH
export GCP_PROJECT=your-project-id
# optional: export GCP_LOCATION=us-central1   (defaults to 'global')
```

If these live in a `.env` file: `uv run --env-file .env journey_capture_genai.py`

## Run

```bash
uv run journey_capture_genai.py                                   # defaults
uv run journey_capture_genai.py "13 00 22 3 004 011.000" 2023     # parcel + year
PARCEL_NUMBER="..." TAX_YEAR=2024 uv run journey_capture_genai.py # via env
```

## Notes

- If your browser-use version rejects `output_model_schema=` on `Agent(...)`,
  try `output_model=` — the kwarg was renamed across versions.
- Money and dates in `ParcelDetail` are raw strings exactly as shown on the
  page ('$1,060.12', '12/29/2023'); parse downstream if you need numbers.
- If the flash-lite model drops rows from large tables (e.g. the 27-row
  historical payments), the next step is replacing Phase 3 with a direct
  GenAI call over the page HTML using `response_schema=ParcelDetail`.
