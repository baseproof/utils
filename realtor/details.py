"""Detail process: captures each queued property's detail page and archives it.

Inventory (main.py) never opens detail pages; it queues every property it finds with a URL by
setting detail.mhtml_uri = null. This process takes properties where detail.mhtml_uri is null,
so each is captured once and reruns skip everything already captured.

For each property:
  1. open full_url and confirm it is that property's detail page (URL path and the page's own
     property_id); nothing is stored unless it is
  2. open every accordion and click the "Show more" / "See more" buttons inside them
  3. archive page.mhtml + property.json in GCS, write-once (archive.py)
  4. write detail.* on properties/{property_id}: the gs:// paths, captured_at, and the raw
     property details as provided (detail_record.py); interpreting them is post-processing

The first property that fails stops the run, after saving the page under debug/ and recording
detail.last_error, so it can be looked at before anything else is captured.
"""
import argparse
import asyncio
import json
import os
import random
import sys
from datetime import datetime, timezone

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

import detail_record
from archive import Archive
from scrape_utils import capture_mhtml, log, smooth_scroll, start_browser
from settings import FIRESTORE_DB, GCP_PROJECT, PROPERTIES

# ==========================================
# 0. TIMING & DELAY CONSTANTS
# ==========================================
DELAY_HESITATION_MIN = 1.0
DELAY_HESITATION_MAX = 2.5
DELAY_EXPAND_SETTLE = 2.5        # price and tax history load when their accordions open
DELAY_BETWEEN_PAGES_MIN = 3.0
DELAY_BETWEEN_PAGES_MAX = 6.0
RECORD_WAIT = 20                 # seconds to wait for the page's property record
CAPTURE_TIMEOUT = 180.0          # max seconds per property

DEBUG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug")

JS_NEXT_DATA = "(document.getElementById('__NEXT_DATA__') || {}).textContent || ''"

# Opens every collapsed accordion, then clicks the "Show more" / "See more" buttons inside the
# accordions and the description's. Buttons only, so nothing navigates away. Returns the click count.
JS_EXPAND = r"""
(async () => {
    const pause = ms => new Promise(r => setTimeout(r, ms));
    const label = el => (el.innerText || '').trim();
    let clicks = 0;
    for (let round = 0; round < 5; round++) {
        const headers = Array.from(document.querySelectorAll('[data-testid="accordion-header"]'));
        const regions = headers.map(h => document.getElementById(h.getAttribute('aria-controls'))).filter(Boolean);
        const more = [
            ...document.querySelectorAll('[data-testid="romance-paragraph-button"]'),
            ...regions.flatMap(r => Array.from(r.querySelectorAll('button'))),
        ].filter(b => /^(show|see) more$/i.test(label(b)) && b.getAttribute('aria-expanded') !== 'true');
        const todo = [...headers.filter(h => h.getAttribute('aria-expanded') === 'false'), ...more];
        if (!todo.length) break;
        for (const el of todo) {
            el.click();
            clicks++;
            await pause(600);
        }
    }
    return clicks;
})()
"""

# ==========================================
# 1. QUEUE
# ==========================================
async def build_queue(db, limit, property_ids, recapture):
    """The given property ids, or the properties inventory queued (detail.mhtml_uri == null)."""
    col = db.collection(PROPERTIES)
    if not property_ids:
        query = col.where(filter=FieldFilter("detail.mhtml_uri", "==", None)).limit(limit)
        return [snap async for snap in query.stream()]
    queue = []
    for pid in property_ids:
        snap = await col.document(pid).get()
        if not snap.exists:
            raise SystemExit(f"❌ properties/{pid} does not exist")
        if (snap.to_dict().get("detail") or {}).get("mhtml_uri") and not recapture:
            log(f"⏭️  {pid} already captured; skipping (use --recapture to capture it again)")
            continue
        queue.append(snap)
    return queue

# ==========================================
# 2. CAPTURE
# ==========================================
async def capture(page, archive, doc_ref, property_id):
    """Archives the open page and writes its fields. Raises DetailPageError, before anything is
    stored, if the page is not this property's detail page."""
    next_data = ""
    for _ in range(RECORD_WAIT):
        next_data = await page.evaluate(JS_NEXT_DATA)
        if next_data:
            break
        await asyncio.sleep(1)
    record = detail_record.property_record(next_data)
    detail_record.check_identity(record, property_id, await page.evaluate("location.pathname"))

    await asyncio.sleep(random.uniform(DELAY_HESITATION_MIN, DELAY_HESITATION_MAX))
    await smooth_scroll(page)
    clicks = await page.evaluate(JS_EXPAND, await_promise=True)
    await asyncio.sleep(DELAY_EXPAND_SETTLE)
    # Expanding only clicks buttons, but confirm the page is still this property before keeping it
    detail_record.check_identity(record, property_id, await page.evaluate("location.pathname"))

    mhtml = await capture_mhtml(page)
    captured_at = datetime.now(timezone.utc)
    url = await page.evaluate("location.href")
    uris = await archive.store_capture(property_id, captured_at, url, mhtml, json.dumps(record))
    fields = detail_record.detail_fields(record, {**uris, "captured_at": captured_at, "url": url})
    fields["detail"]["last_error"] = firestore.DELETE_FIELD
    await doc_ref.set(fields, merge=True)
    return uris, clicks

async def record_failure(page, doc_ref, property_id, error):
    """Keeps a local copy of whatever the browser showed and notes the error on the property."""
    stamp = datetime.now(timezone.utc)
    if page is not None:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        path = os.path.join(DEBUG_DIR, f"{property_id}_{stamp.strftime('%Y-%m-%dT%H%M%SZ')}.mhtml")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(await capture_mhtml(page))
            log(f"   🧾 Saved the page to {path}")
        except Exception as e:
            log(f"   ⚠️ Could not save the page: {e}")
    await doc_ref.set({"detail": {"last_error": {"at": stamp, "reason": f"{type(error).__name__}: {error}"}}}, merge=True)

# ==========================================
# 3. ORCHESTRATION LOOP
# ==========================================
async def run(limit, property_ids, recapture):
    db = firestore.AsyncClient(project=GCP_PROJECT, database=FIRESTORE_DB)
    queue = await build_queue(db, limit, property_ids, recapture)
    if not queue:
        log("✅ No properties waiting for a detail capture.")
        return 0

    archive = Archive()
    log(f"🚀 Capturing {len(queue)} detail page(s)...")
    browser = await start_browser()
    captured = 0
    try:
        for idx, snap in enumerate(queue):
            property_id, url = snap.id, snap.to_dict().get("full_url")
            log(f"\n--- [{idx + 1}/{len(queue)}] {property_id} | {url} ---")
            if not url:
                log(f"🛑 properties/{property_id} has no full_url; stopped after {captured} capture(s).")
                return 1

            page = None
            try:
                page = await browser.get(url)
                uris, clicks = await asyncio.wait_for(capture(page, archive, snap.reference, property_id), timeout=CAPTURE_TIMEOUT)
            except Exception as e:
                log(f"   🛑 {type(e).__name__}: {e}")
                await record_failure(page, snap.reference, property_id, e)
                log(f"🛑 Stopped at {property_id} after {captured} capture(s); it stays queued for the next run.")
                return 1

            captured += 1
            log(f"   🖱️  Expanded {clicks} section(s)")
            log(f"   💾 {uris['mhtml_uri']}")

            if idx < len(queue) - 1:
                delay = random.uniform(DELAY_BETWEEN_PAGES_MIN, DELAY_BETWEEN_PAGES_MAX)
                log(f"   ⏳ Waiting {delay:.2f}s before next property...")
                await asyncio.sleep(delay)
    finally:
        browser.stop()

    log(f"\n🏁 Captured {captured} detail page(s).")
    return 0

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Realtor.com detail-page capture (Firestore queue -> GCS + Firestore)")
    parser.add_argument("--limit", type=int, default=50, help="Max properties to capture from the queue this run (default 50)")
    parser.add_argument("--property-ids", type=str, default=None, help="Optional: Comma-separated property ids to capture instead of the queue")
    parser.add_argument("--recapture", action="store_true", help="With --property-ids: capture again even if already captured (adds a new capture)")

    args = parser.parse_args()

    if args.limit < 1:
        parser.error("--limit must be at least 1")
    if args.recapture and not args.property_ids:
        parser.error("--recapture requires --property-ids")
    ids = [p.strip() for p in args.property_ids.split(",") if p.strip()] if args.property_ids else []

    try:
        sys.exit(asyncio.run(run(args.limit, ids, args.recapture)))
    except KeyboardInterrupt:
        log("\nStopped by user.")
