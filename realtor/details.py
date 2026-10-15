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

Several workers can run at once (--workers, default in tuning.toml), each with its own browser,
taking properties from one shared list so none is opened twice. The first failure stops every
worker (each finishes the page it has open), after saving the page under debug/ and recording
detail.last_error, so it can be looked at before anything else is captured.
"""
import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

import detail_record
from archive import Archive
from scrape_utils import capture_mhtml, log, smooth_scroll, start_browser
from settings import FIRESTORE_DB, GCP_PROJECT, PROPERTIES
from tuning import TUNING, wait

T = TUNING.details
DEBUG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug")

JS_NEXT_DATA = "(document.getElementById('__NEXT_DATA__') || {}).textContent || ''"

# Opens every collapsed accordion, then clicks the "Show more" / "See more" buttons inside the
# accordions and the description's. Buttons only, so nothing navigates away. Called with the
# pause between clicks in ms; returns the click count.
JS_EXPAND = r"""
(async (pauseMs) => {
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
            await pause(pauseMs);
        }
    }
    return clicks;
})
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
    next_data, waited = await page.evaluate(JS_NEXT_DATA), 0
    while not next_data and waited < T.record_wait:
        await asyncio.sleep(1)
        waited += 1
        next_data = await page.evaluate(JS_NEXT_DATA)
    record = detail_record.property_record(next_data)
    detail_record.check_identity(record, property_id, await page.evaluate("location.pathname"))

    await asyncio.sleep(wait(T.hesitation))
    await smooth_scroll(page)
    clicks = await page.evaluate(f"{JS_EXPAND}({round(T.expand_click_pause * 1000)})", await_promise=True)
    await asyncio.sleep(T.expand_settle)
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
# 3. WORKERS
# ==========================================
class Progress:
    def __init__(self, total):
        self.total, self.started, self.captured, self.failed = total, 0, 0, None

async def worker(name, browser, archive, todo, progress, stop):
    """Captures properties from `todo` until it is empty or any worker has failed."""
    while not stop.is_set():
        try:
            snap = todo.get_nowait()
        except asyncio.QueueEmpty:
            return
        progress.started += 1
        property_id, url = snap.id, snap.to_dict().get("full_url")
        log(f"--- {name} [{progress.started}/{progress.total}] {property_id} | {url} ---")

        page = None
        try:
            if not url:
                raise detail_record.DetailPageError("the property has no full_url")
            page = await browser.get(url)
            uris, clicks = await asyncio.wait_for(capture(page, archive, snap.reference, property_id), timeout=T.capture_timeout)
        except Exception as e:
            stop.set()   # no worker starts another property
            progress.failed = property_id
            log(f"   🛑 {name} {property_id}: {type(e).__name__}: {e}")
            await record_failure(page, snap.reference, property_id, e)
            return

        progress.captured += 1
        log(f"   💾 {name} {property_id}: expanded {clicks} section(s) -> {uris['mhtml_uri']}")
        if not todo.empty() and not stop.is_set():
            await asyncio.sleep(wait(T.between_pages))

async def run(limit, property_ids, recapture, workers):
    db = firestore.AsyncClient(project=GCP_PROJECT, database=FIRESTORE_DB)
    queue = await build_queue(db, limit, property_ids, recapture)
    if not queue:
        log("✅ No properties waiting for a detail capture.")
        return 0

    workers = min(workers, len(queue))
    todo: asyncio.Queue = asyncio.Queue()
    for snap in queue:
        todo.put_nowait(snap)
    progress, stop = Progress(len(queue)), asyncio.Event()
    log(f"🚀 Capturing {len(queue)} detail page(s) with {workers} worker(s)...")

    browsers = []
    try:
        for _ in range(workers):   # one at a time, see start_browser
            browsers.append(await start_browser())
        await asyncio.gather(*(worker(f"w{i + 1}", browser, Archive(), todo, progress, stop)
                               for i, browser in enumerate(browsers)))
    finally:
        for browser in browsers:
            browser.stop()

    if progress.failed:
        log(f"\n🛑 Stopped after {progress.captured} capture(s): {progress.failed} failed and stays queued, "
            f"as do the {todo.qsize()} not started.")
        return 1
    log(f"\n🏁 Captured {progress.captured} detail page(s).")
    return 0

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Realtor.com detail-page capture (Firestore queue -> GCS + Firestore)")
    parser.add_argument("--limit", type=int, default=50, help="Max properties to capture from the queue this run (default 50)")
    parser.add_argument("--workers", type=int, default=T.workers, help=f"Browsers capturing in parallel (default {T.workers}, from tuning.toml)")
    parser.add_argument("--property-ids", type=str, default=None, help="Optional: Comma-separated property ids to capture instead of the queue")
    parser.add_argument("--recapture", action="store_true", help="With --property-ids: capture again even if already captured (adds a new capture)")

    args = parser.parse_args()

    if args.limit < 1:
        parser.error("--limit must be at least 1")
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    if args.recapture and not args.property_ids:
        parser.error("--recapture requires --property-ids")
    ids = [p.strip() for p in args.property_ids.split(",") if p.strip()] if args.property_ids else []

    try:
        sys.exit(asyncio.run(run(args.limit, ids, args.recapture, args.workers)))
    except KeyboardInterrupt:
        log("\nStopped by user.")
