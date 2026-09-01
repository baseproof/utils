import asyncio
import os
import sys
import yaml
from pathlib import Path

# Required dependency checks
try:
    from browser_use import Agent, Browser
    from browser_use.llm import ChatGoogle  # Native Google GenAI SDK integration
    from google.oauth2 import service_account
except ImportError as e:
    print(f'❌ Missing dependencies: {e}')
    print('Please install requirements using uv:')
    print('uv pip install browser-use google-genai google-auth pyyaml pydantic')
    sys.exit(1)

from parcel_models import ParcelDetail

DEFAULT_PARCEL_NUMBER = '20 00 01 1 001 003.000'
DEFAULT_TAX_YEAR = '2025'
MODEL_NAME = 'gemini-3.5-flash-lite'


def get_adc_credentials(json_path: str | None = None) -> service_account.Credentials | None:
    """
    Load Google Application Default Credentials (ADC) from a JSON service account file
    or fallback to environment variable GOOGLE_APPLICATION_CREDENTIALS.
    """
    credential_path = json_path or os.getenv('GOOGLE_APPLICATION_CREDENTIALS')

    if credential_path and os.path.exists(credential_path):
        print(f'🔑 Loading ADC JSON credentials from: {credential_path}')
        credentials = service_account.Credentials.from_service_account_file(
            credential_path,
            scopes=['https://www.googleapis.com/auth/cloud-platform'],
        )
        return credentials
    else:
        print('⚠️ GOOGLE_APPLICATION_CREDENTIALS path not set or file not found.')
        print('Falling back to standard Google Auth default credentials context (ADC chain)...')
        return None


def init_gemini_llm(json_credentials_path: str | None = None) -> ChatGoogle:
    """
    Initialize Gemini via browser-use's native ChatGoogle,
    which is backed by the Google GenAI SDK (genai.Client) in Vertex AI mode.
    """
    project_id = os.getenv('GCP_PROJECT') or os.getenv('GOOGLE_CLOUD_PROJECT')
    location = os.getenv('GCP_LOCATION', 'global')

    credentials = get_adc_credentials(json_credentials_path)

    return ChatGoogle(
        model=MODEL_NAME,
        vertexai=True,
        project=project_id,
        location=location,
        credentials=credentials,
        temperature=0.0,
    )


# ---------------------------------------------------------------------------
# Phase tasks — ONE goal per agent run, all on the SAME browser session.
# Every task ends with a fixed sentinel: 'OK: <phase>' on success, or
# 'FAILED: <reason>' — the pipeline gates on it.
# ---------------------------------------------------------------------------

def phase_open_portal() -> str:
    return """
    Goal: open the property search page. Do ONLY this.
    1. Navigate to 'https://eringcapture.jccal.org/'
    2. Pause for 5 seconds to allow the page to stabilize fully.
    3. Dismiss any dialogs, popups, or disclaimer modals on screen.
    4. Click on 'Real Property Search'.
    5. Pause for 3 seconds. Confirm the real property search page is showing
       (it has a search type dropdown and a search input field).
    Finish and report 'OK: search page open' if confirmed,
    otherwise 'FAILED: <what you see instead>'.
    """


def phase_set_dropdowns(tax_year: str) -> str:
    return f"""
    You are on the real property search page. Goal: set the two dropdowns.
    Do ONLY this — do not type in the search field yet.
    1. Find the search type dropdown and select 'Parcel #' from it.
    2. Find the year dropdown and select '{tax_year}' from it.
    3. Confirm both dropdowns now show the selected values.
    Finish and report 'OK: dropdowns set' if confirmed,
    otherwise 'FAILED: <which dropdown and what it shows>'.
    """


def phase_enter_parcel(parcel_number: str) -> str:
    return f"""
    You are on the real property search page with search type 'Parcel #'
    already selected. Goal: enter the parcel number and run the search.
    1. Click the search input field and type x_parcel_digits into it.
       The field will automatically insert a period in the right place.
    2. Verify the field now shows exactly this value: {parcel_number}
       If it shows anything else, clear the field once and type
       x_parcel_digits again.
    3. An autocomplete suggestion may appear below the field. If a suggestion
       exactly matching {parcel_number} appears, click that suggestion.
       Otherwise, press the submit / search button.
    4. Wait up to 5 seconds for search results to appear.
    Finish and report 'OK: search submitted' once results (or an empty-results
    message) are visible, otherwise 'FAILED: <reason>'.
    """


def phase_open_detail(parcel_number: str) -> str:
    return f"""
    You are on the search results page. Goal: open the exact matching parcel.
    1. Read the Parcel # value of each result row.
    2. Find the row whose Parcel # matches exactly: {parcel_number}
       Compare character-by-character. Do NOT click a row that is only similar.
    3. If an exact match exists, click that row's 'View Details' link.
    4. Confirm the detail page loaded: it shows a heading
       'Detail for Parcel: {parcel_number}'.
    Finish and report 'OK: detail page open' if confirmed.
    If no results or no exact match, click nothing and report
    'FAILED: no exact match for {parcel_number}'.
    """


def phase_verify_and_expand(parcel_number: str) -> str:
    return f"""
    You should be on a parcel detail page. Do NOT navigate anywhere else.

    0. FIRST, verify you are on the correct property detail page. Check ALL of:
       - A heading that says exactly: 'Detail for Parcel: {parcel_number}'
       - An 'Owner:' label with an owner name
       - A 'Valuation Summary' section further down the page
       Also check the current URL path contains the word: parceldetail
       If ANY check fails, do not click anything, finish immediately, and
       report 'FAILED: <which check failed and what you see>'.

    1. Once verified: scroll down and find 'View Historical Tax Payments'.
       If present, click it to expand the historical payments table.
       If it already says 'Hide Historical Tax Payments', it is already
       expanded — do not click it again.
    2. Find 'View Instruments'. If present, click it to expand the Legal
       Instruments table. If it already says 'Hide Instruments', it is
       already expanded — do not click it again.
    3. Confirm the labels now read 'Hide Historical Tax Payments' and
       'Hide Instruments'.
    Finish and report 'OK: verified and expanded' if confirmed,
    otherwise 'FAILED: <reason>'.
    """


def phase_extract(parcel_number: str) -> str:
    return f"""
    You are on the fully expanded parcel detail page for {parcel_number}.
    Do NOT navigate anywhere else and do NOT click anything.
    Scroll through the ENTIRE page from top to bottom and extract every piece
    of information: parcel identity, owner, property and mailing addresses,
    assessment info, valuation summary, full tax breakdown (all rows, fees,
    and totals), all tax payments (current AND historical — every row),
    land information, every building section (general info, building value,
    appendages, construction units, extra features), deed information,
    ownership history (every row), and legal instruments.
    Do not skip any table rows. Return the data in the required structured
    output format.
    """


# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------

def collect_trace(history) -> list[dict]:
    """Build a step trace from a browser-use AgentHistoryList."""
    steps = []
    for step_idx, step in enumerate(history.history, start=1):
        model_output = getattr(step, 'model_output', None)

        actions = []
        if model_output and getattr(model_output, 'action', None):
            actions = [act.model_dump(exclude_none=True) for act in model_output.action]

        thought = None
        if model_output:
            thought = (
                getattr(model_output, 'thinking', None)
                or getattr(model_output, 'thought', None)
                or getattr(model_output, 'next_goal', None)
            )

        state = getattr(step, 'state', None)
        steps.append({
            'step': step_idx,
            'thought': thought,
            'actions': actions,
            'current_url': getattr(state, 'url', None) if state else None,
        })
    return steps


async def run_phase(name: str, task: str, llm, browser, phases: list[dict], **agent_kwargs) -> tuple[bool, str]:
    """
    Run one agent phase on the shared browser. Returns (success, final_result).
    Success requires the agent to finish AND not report 'FAILED'.
    """
    print(f'\n🎯 Phase: {name}...')
    agent = Agent(task=task, llm=llm, browser=browser, use_vision=True, **agent_kwargs)
    history = await agent.run()
    final = str(history.final_result() or '')
    ok = history.is_done() and 'FAILED' not in final
    phases.append({
        'name': name,
        'status': 'completed' if ok else 'failed',
        'final_result': final,
        'trace': collect_trace(history),
    })
    print(f"{'✅' if ok else '⛔'} Phase '{name}': {final[:120]}")
    return ok, final


async def close_browser(browser) -> None:
    """Close the shared browser session, tolerating API differences."""
    for method_name in ('kill', 'stop', 'close'):
        method = getattr(browser, method_name, None)
        if method:
            try:
                result = method()
                if asyncio.iscoroutine(result):
                    await result
                return
            except Exception:
                continue


async def main():
    # Parcel number & year: CLI args > env vars > defaults
    parcel_number = (
        sys.argv[1] if len(sys.argv) > 1
        else os.getenv('PARCEL_NUMBER', DEFAULT_PARCEL_NUMBER)
    )
    tax_year = (
        sys.argv[2] if len(sys.argv) > 2
        else os.getenv('TAX_YEAR', DEFAULT_TAX_YEAR)
    )

    print(f'🚀 Initializing Browser-Use with {MODEL_NAME} (Google GenAI SDK)...')
    print(f'📋 Target parcel: {parcel_number} | Year: {tax_year}')

    json_credentials_path = os.getenv('ADC_JSON_PATH') or os.getenv('GOOGLE_APPLICATION_CREDENTIALS')
    llm = init_gemini_llm(json_credentials_path)

    # ONE browser session shared across all phases, locked to the county site
    # (required for safe sensitive_data usage, and prevents any phase from
    # wandering off-domain).
    browser = Browser(
        headless=False,
        keep_alive=True,
        allowed_domains=['https://eringcapture.jccal.org'],
    )

    phases: list[dict] = []
    parcel_data = None

    try:
        # Each phase is a small, single-goal agent run. The pipeline stops at
        # the first failed phase.
        pipeline = [
            ('open_portal', phase_open_portal(), {}),
            ('set_dropdowns', phase_set_dropdowns(tax_year), {}),
            ('enter_parcel', phase_enter_parcel(parcel_number),
             {'sensitive_data': {'x_parcel_digits': parcel_number.replace('.', '')}}),
            ('open_detail', phase_open_detail(parcel_number), {}),
            ('verify_and_expand', phase_verify_and_expand(parcel_number), {}),
        ]

        all_ok = True
        for name, task, kwargs in pipeline:
            ok, _ = await run_phase(name, task, llm, browser, phases, **kwargs)
            if not ok:
                all_ok = False
                break

        if all_ok:
            # Final phase: structured extraction (schema only attached here).
            ok, final = await run_phase(
                'extract', phase_extract(parcel_number), llm, browser, phases,
                output_model_schema=ParcelDetail,
            )
            if ok and final:
                try:
                    parcel_data = ParcelDetail.model_validate_json(final)
                    print('📦 Structured ParcelDetail parsed successfully.')
                except Exception as e:
                    print(f'⚠️ Could not parse structured output as ParcelDetail: {e}')
    finally:
        await close_browser(browser)

    # ---------------- Save outputs ----------------
    journey_manifest = {
        'target_url': 'https://eringcapture.jccal.org/',
        'parcel_number': parcel_number,
        'tax_year': tax_year,
        'model': MODEL_NAME,
        'sdk': 'google-genai (Vertex AI mode)',
        'status': 'completed' if parcel_data else ('partial' if phases else 'failed'),
        'parcel_detail': parcel_data.model_dump() if parcel_data else None,
        'phases': phases,
    }

    output_dir = Path('utils')
    output_dir.mkdir(parents=True, exist_ok=True)
    yaml_path = output_dir / 'journey_capture.yaml'

    with open(yaml_path, 'w', encoding='utf-8') as f:
        yaml.dump(journey_manifest, f, default_flow_style=False, sort_keys=False)

    if parcel_data:
        json_path = output_dir / 'parcel_detail.json'
        json_path.write_text(parcel_data.model_dump_json(indent=2), encoding='utf-8')
        print(f'📦 Structured parcel data saved to: {json_path}')

    print(f'\n✅ Journey finished ({journey_manifest["status"]}). Trace saved to: {yaml_path}')


if __name__ == '__main__':
    asyncio.run(main())