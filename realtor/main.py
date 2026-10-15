"""Inventory: search result pages -> properties in Firestore. Never opens a detail page.

Every property found with a URL is queued for details.py (detail.mhtml_uri = null); field
ownership per stage is listed in settings.py.
"""
import asyncio
import json
import os
import re
import sys
import argparse
from datetime import datetime, timezone

# Add the Google Cloud Firestore import
from google.cloud import firestore

from scrape_utils import capture_mhtml, log, smooth_scroll, start_browser
from settings import FIRESTORE_DB, GCP_PROJECT, PROPERTIES, SCRAPE_LOGS
from tuning import TUNING, wait

# ==========================================
# 0. TIMING & DELAY CONSTANTS (Configurable)
# ==========================================

T = TUNING.inventory   # waits live in tuning.toml [inventory]

# ==========================================
# 1. CONFIGURATION & SETUP
# ==========================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOCATIONS_FILE = os.path.join(SCRIPT_DIR, "locations.json")
BASE_URL = "https://www.realtor.com/realestateandhomes-search"

# Search filters, as the filter panel writes them into the URL path:
#   .../Birmingham_AL/type-single-family-home,condo/shw-eh/pg-2
# realtor.com silently ignores unknown codes and returns unfiltered results, so only
# these names are accepted. Codes were confirmed against live search pages.
HOME_TYPES = {                        # "Home type" (any number, comma-joined)
    "house": "single-family-home",
    "condo": "condo",
    "townhome": "townhome",
    "multi-family": "multi-family-home",
    "mobile": "mfd-mobile-home",
    "land": "land",
    "farm": "farms-ranches",
}
LISTING_TYPES = {                     # "Listing details -> Type" (one only; "shw-eh,fc" is ignored)
    "existing": "eh",
    "foreclosure": "fc",
    "new-construction": "nc",
    "55-plus": "55p",
    "auction": "au",
}

def load_locations():
    if not os.path.exists(LOCATIONS_FILE):
        log(f"Error: {LOCATIONS_FILE} not found. Please ensure it is in the same directory.")
        sys.exit(1)
    with open(LOCATIONS_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

# ==========================================
# 2. UTILITY & HELPER FUNCTIONS
# ==========================================

def parse_page_ranges(page_string):
    pages: set[int] = set()
    for part in page_string.split(','):
        part = part.strip()
        if '-' in part:
            start, end = map(int, part.split('-'))
            pages.update(range(start, end + 1))
        elif part.isdigit():
            pages.add(int(part))
    return sorted(list(pages))

def limit_reached(collected, limit):
    return limit is not None and len(collected) >= limit

def build_filter_path(home_types, listing_type):
    """Returns the filter path segments, e.g. 'type-single-family-home,condo/shw-eh', or ''."""
    segments = []
    if home_types:
        segments.append("type-" + ",".join(HOME_TYPES[t] for t in home_types))
    if listing_type:
        segments.append("shw-" + LISTING_TYPES[listing_type])
    return "/".join(segments)

def search_url(slug, filter_path="", page_num=None):
    parts = [BASE_URL, slug, filter_path, f"pg-{page_num}" if page_num else ""]
    return "/".join(p for p in parts if p)

def card_changed(existing, card):
    """True if any card value differs from what is stored (fields other stages own are ignored)."""
    for key, value in card.items():
        if isinstance(value, dict) and isinstance(existing.get(key), dict):
            if card_changed(existing[key], value):
                return True
        elif existing.get(key) != value:
            return True
    return False

def inventory_fields(card, existing, home_type, seen_at):
    """What inventory writes for one card (merged): the card plus inventory's own bookkeeping.
    A property with a URL and no detail capture yet is queued with detail.mhtml_uri = null."""
    fields = dict(card)
    fields["inventory"] = {
        "first_seen_at": (existing.get("inventory") or {}).get("first_seen_at") or seen_at,
        "last_seen_at": seen_at,
    }
    if home_type:
        fields["home_types"] = sorted(set(existing.get("home_types") or []) | {home_type})
    if card.get("full_url") and "mhtml_uri" not in (existing.get("detail") or {}):
        fields["detail"] = {"mhtml_uri": None, "captured_at": None}
    return fields

def merge_by_property(cards):
    """One entry per property for --output; a property found by two type passes keeps both types."""
    merged: dict[str, dict] = {}
    for card in cards:
        entry = merged.setdefault(card["property_id"], card)
        if entry is not card:
            entry["home_types"] = sorted(set(entry.get("home_types", [])) | set(card.get("home_types", [])))
    return list(merged.values())

async def save_mhtml(page, filename="error_page.mhtml"):
    try:
        log(f"--> Saving page state to {filename}...")
        snapshot = await capture_mhtml(page)
        with open(filename, "w", encoding="utf-8") as f:
            f.write(snapshot)
    except Exception as e:
        log(f"--> Failed to save MHTML: {e}. Falling back to standard HTML...")
        html = await page.get_content()
        with open(filename.replace(".mhtml", ".html"), "w", encoding="utf-8") as f:
            f.write(html)

# What a search page says about itself: the "Next ›" link of its pagination and the total
# realtor.com shows. Pages are walked by following Next; the total is recorded only to compare.
JS_PAGE_STATE = """
    (() => {
        const nav = document.querySelector('nav[data-testid="pagination"]');
        const next = nav ? Array.from(nav.querySelectorAll('a[href]')).find(a => /^next/i.test((a.innerText || '').trim())) : null;
        const totalEl = document.querySelector('[data-testid="total-results"]');
        const shown = totalEl ? parseInt(totalEl.innerText.replace(/[^0-9]/g, ''), 10) : NaN;
        return JSON.stringify({next_href: next ? next.getAttribute('href') : null, shown: Number.isNaN(shown) ? null : shown});
    })()
"""

# Cards are on the page, or the page says it has no results
JS_RESULTS_READY = """
    !!document.querySelector('[data-testid="property-card"]')
    || ((document.querySelector('[data-testid="total-results"]') || {}).innerText || '').trim() === '0'
    || /(^|\\D)0 results that match your search/.test((document.body && document.body.innerText) || '')
"""

def next_page_number(next_href, page_num):
    """The page the Next link leads to: None on the last page. A Next link to anywhere but the
    following page is markup this code doesn't understand, so it raises."""
    if not next_href:
        return None
    m = re.search(r"/pg-(\d+)", next_href)
    if not m or int(m.group(1)) != page_num + 1:
        raise ValueError(f"page {page_num}'s Next link points to {next_href}")
    return page_num + 1


# ==========================================
# 3. CORE SCRAPER LOGIC
# ==========================================

async def run_master_scraper(browser, target_slug, pages_str, rerun, collected, limit, filter_path, home_type, db):
    """Main orchestration function relying on the globally passed browser.

    Every kept card is appended to `collected`; `limit` caps its size. With `db` the cards are
    written to Firestore; without it (--output) Firestore is never touched. `filter_path` (from
    build_filter_path) narrows the search, and `home_type` is the one home type this pass
    searches, recorded on every property it finds. Each pass is logged in scrape_logs under its
    own key, and only a full pass (every page, limit not hit, no failed page) is marked completed.
    """
    if not target_slug:
        log("⚠️ Invalid target_slug provided. Skipping.")
        return False, browser

    slug = str(target_slug)
    log_key = f"{slug}__{filter_path.replace('/', '__')}" if filter_path else slug
    log_ref = db.collection(SCRAPE_LOGS).document(log_key) if db else None

    if log_ref and not rerun:
        log_doc = await log_ref.get()
        if log_doc.exists and log_doc.to_dict().get("status") == "completed":
            log(f"✅ {slug} previously completed. Skipping to maintain single-pass. (Use --rerun to bypass)")
            return False, browser

    if log_ref:
        await log_ref.set({"status": "in_progress"}, merge=True)
    
    # --- JAVASCRIPT PAYLOAD (Defined once outside loop) ---
    js_extract_script = """
        (() => {
            const properties = [];
            const cards = document.querySelectorAll('[data-testid="property-card"]');

            cards.forEach(card => {
                try {
                    const isExpanded = card.closest('[data-testid="expanded-property-list"]');
                    if (isExpanded) return;

                    const linkEl = card.querySelector('a[href*="/realestateandhomes-detail/"]');
                    const full_url = linkEl ? "https://www.realtor.com" + linkEl.getAttribute('href').split('?')[0] : "";
                    const url_slug = full_url.split('/').pop() || "";

                    // Cards no longer carry data-property-id / data-listing-id. The detail URL ends in
                    // _M12345-67890 (listing) or _P123456789 (new-construction plan), which is the property_id.
                    // listing_id is not on the card at all, so it is left out rather than written as null.
                    const idMatch = url_slug.match(/_(?:M([0-9]+)-([0-9]+)|P([0-9]+))$/);
                    const property_id = idMatch ? (idMatch[1] ? idMatch[1] + idMatch[2] : idMatch[3]) : null;

                    const priceEl = card.querySelector('[data-rui="Price"]');
                    const list_price = priceEl ? parseInt(priceEl.innerText.replace(/[^0-9]/g, '')) : null;

                    const bedsEl = card.querySelector('[data-testid="property-meta-beds"] [data-testid="meta-value"]');
                    const beds = bedsEl ? parseFloat(bedsEl.innerText) : null;

                    const bathsEl = card.querySelector('[data-testid="property-meta-baths"] [data-testid="meta-value"]');
                    const baths = bathsEl ? parseFloat(bathsEl.innerText) : null;

                    const sqftEl = card.querySelector('[data-testid="property-meta-sqft"] [data-testid="meta-value"]');
                    const sqft = sqftEl ? parseInt(sqftEl.innerText.replace(/[^0-9]/g, '')) : null;

                    const lotEl = card.querySelector('[data-testid="property-meta-lot-size"] [data-testid="meta-value"]');
                    let lot_sqft = null;
                    if (lotEl) {
                        const lotText = lotEl.closest('li').innerText.toLowerCase();
                        const lotVal = parseFloat(lotEl.innerText.replace(/,/g, ''));
                        if (lotText.includes('acre')) {
                            lot_sqft = Math.round(lotVal * 43560);
                        } else {
                            lot_sqft = lotVal;
                        }
                    }
                    
                    // The address lines are no longer labelled; the card link's aria-label carries the
                    // whole address: "View details for 921 Village Mill Rd, Birmingham, AL 35215"
                    const label = linkEl ? (linkEl.getAttribute('aria-label') || '').replace(/^View details for /, '') : "";
                    const labelParts = label.split(',');
                    const line = labelParts.length >= 3 ? labelParts.slice(0, -2).join(',').trim() : "";
                    const addr2 = labelParts.length >= 3 ? labelParts.slice(-2).join(',').trim() : "";
                    
                    let city = "", state_code = "", postal_code = "";
                    if (addr2) {
                        const parts = addr2.split(',');
                        if (parts.length === 2) {
                            city = parts[0].trim();
                            const stateZip = parts[1].trim().split(' ');
                            if (stateZip.length >= 2) {
                                state_code = stateZip[0].trim();
                                postal_code = stateZip[1].trim();
                            }
                        }
                    }
                    
                    // Status ("House for sale") is the unlabelled block just before the price block
                    const statusEl = priceEl ? priceEl.parentElement.previousElementSibling : null;
                    const status = statusEl ? statusEl.innerText.trim() : "";

                    const brokerEl = card.querySelector('[data-testid="broker-title"]');
                    const broker = brokerEl ? brokerEl.innerText.replace('Brokered by', '').trim() : "";

                    properties.push({
                        property_id: property_id,
                        full_url: full_url,
                        url_slug: url_slug,
                        list_price: isNaN(list_price) ? null : list_price,
                        description: {
                            beds: isNaN(beds) ? null : beds,
                            baths: isNaN(baths) ? null : baths,
                            sqft: isNaN(sqft) ? null : sqft,
                            lot_sqft: isNaN(lot_sqft) ? null : lot_sqft
                        },
                        location: {
                            address: {
                                line: line,
                                city: city,
                                state_code: state_code,
                                postal_code: postal_code
                            }
                        },
                        status: status,
                        broker: broker
                    });
                } catch (e) {
                    console.error(e);
                }
            });
            
            return JSON.stringify(properties);
        })()
    """

    try:
        explicit_pages = parse_page_ranges(pages_str) if pages_str else None

        log(f"\nTargeting Location: {slug}")
        log(f"Filters: {filter_path or 'none'}" + (f" (home type: {home_type})" if home_type else ""))
        log(f"Target Pages: {explicit_pages if explicit_pages is not None else 'page 1 onward, following each Next link'}")

        # Sub-function to isolate per-page logic for timeout enforcement. Returns what the page
        # says about itself: the page its Next link leads to (None on the last page), the total
        # realtor.com shows, and how many properties were kept.
        async def scrape_single_page(p_num):
            url = search_url(slug, filter_path, p_num)
            log(f"\n--- Scraping Page {p_num} | {url} ---")
            
            page = await browser.get(url)
            
            log("Waiting for page load and bot protection clearance...")
            page_ready = False
            for _ in range(T.poll_retries): 
                if await page.evaluate(JS_RESULTS_READY) is True:
                    page_ready = True
                    break
                await asyncio.sleep(T.poll_interval) 
            
            if not page_ready:
                log(f"Timeout: Could not find property data. Bot protection likely triggered or page is empty.")
                await save_mhtml(page, f"failed_bot_challenge_{slug}_pg{p_num}.mhtml")
                # Force a TimeoutError to trigger the browser restart block below
                raise asyncio.TimeoutError("Bot protection/empty page detected") 
            
            if not await page.evaluate("!!document.querySelector('[data-testid=\"property-card\"]')"):
                log("realtor.com shows 0 results for this search.")
                return None, 0, 0
            
            await asyncio.sleep(wait(T.hesitation)) 
            
            log("Scrolling page to trigger lazy loading...")
            await smooth_scroll(page)
            
            log("Extracting payload from DOM...")
            json_payload = await page.evaluate(js_extract_script)
            properties_list = json.loads(json_payload) if json_payload else []
            state = json.loads(await page.evaluate(JS_PAGE_STATE))
            next_page = next_page_number(state["next_href"], p_num)

            # Cards without a property_id can't be keyed; the limit counts what is kept
            cards = [p for p in properties_list if p.get('property_id')]
            if not cards:
                await save_mhtml(page, f"extraction_failed_{slug}_pg{p_num}.mhtml")
                raise ValueError(f"page {p_num} shows property cards but none could be read")
            room = None if limit is None else limit - len(collected)
            kept = cards[:room]
            collected.extend(kept)

            if db is None:
                if home_type:
                    for card in kept:
                        card["home_types"] = [home_type]
                log(f"Page {p_num}: kept {len(kept)} of {len(properties_list)} extracted properties for local output.")
            else:
                log(f"Extracted {len(properties_list)} exact matches; saving {len(kept)}. Checking Firestore for changes...")
                seen_at = datetime.now(timezone.utc)
                batch = db.batch()
                batch_count = 0
                unchanged_count = 0
                new_ids = []
                updated_ids = []
                
                for card in kept:
                    pid = str(card['property_id'])
                    doc_ref = db.collection(PROPERTIES).document(pid)
                    doc_snapshot = await doc_ref.get()
                    existing_data = doc_snapshot.to_dict() if doc_snapshot.exists else {}
                    
                    if not doc_snapshot.exists:
                        new_ids.append(pid)
                    elif card_changed(existing_data, card):
                        updated_ids.append(pid)
                    else:
                        unchanged_count += 1
                    # Always written: inventory.last_seen_at records every sighting
                    batch.set(doc_ref, inventory_fields(card, existing_data, home_type, seen_at), merge=True)
                    batch_count += 1
                    
                    if batch_count == 500:
                        await batch.commit()
                        batch = db.batch()
                        batch_count = 0

                if batch_count > 0:
                    await batch.commit()
                    
                log(f"Page {p_num} Summary: {len(new_ids)} NEW, {len(updated_ids)} UPDATED, {unchanged_count} UNCHANGED.")
                if updated_ids:
                    log(f"   -> Updated IDs: {updated_ids}")
                if new_ids:
                    log(f"   -> New IDs: {new_ids}")

            return next_page, state["shown"], len(kept)

        hit_limit = False
        failed_page = None
        pages_done, collected_pass, shown = 0, 0, None
        index = 0
        page_num = explicit_pages[0] if explicit_pages else 1
        while True:
            # Execute the page scrape with retry and restart logic
            result = None
            for attempt in range(2):
                try:
                    result = await asyncio.wait_for(scrape_single_page(page_num), timeout=T.page_timeout)
                    break # Success, break out of retry loop
                except asyncio.TimeoutError:
                    if attempt == 0:
                        log(f"⚠️ LIMIT HIT / TIMEOUT: Page {page_num} exceeded limits. Restarting browser to clear session...")
                        try:
                            browser.stop()
                        except:
                            pass
                        await asyncio.sleep(T.restart_pause) # Give process a moment to close
                        
                        # Spin up fresh instance
                        browser = await start_browser()
                        log(f"Browser restarted. Retrying Page {page_num}...")
                    else:
                        log(f"⚠️ TIMEOUT: Page {page_num} failed again after restart.")
                except Exception as e:
                    log(f"❌ ERROR processing page {page_num}: {e}")
                    break

            if result is None:
                # Later pages are only reachable through this page's Next link
                failed_page = page_num
                log(f"🛑 Stopping this search at page {page_num}.")
                break

            next_page, page_shown, kept_count = result
            pages_done += 1
            collected_pass += kept_count
            if page_shown is not None:
                shown = page_shown
            log(f"Search so far: {pages_done} page(s), {collected_pass} properties"
                + (f" of {shown} shown by realtor.com" if shown is not None else ""))

            if limit_reached(collected, limit):
                log(f"Reached --limit of {limit} properties. Stopping.")
                hit_limit = True
                break

            if explicit_pages is not None:
                index += 1
                if index == len(explicit_pages):
                    break
                page_num = explicit_pages[index]
            elif next_page is None:
                log(f"Page {page_num} has no Next link: last page.")
                break
            else:
                page_num = next_page

            # Keep the spacing delay outside the timeout logic so we always throttle correctly
            delay = wait(T.between_pages)
            log(f"Graceful spacing: Waiting {delay:.2f} seconds before loading the next page...")
            await asyncio.sleep(delay)
        
        counts = {"pages": pages_done, "collected": collected_pass, "results_shown": shown}
        full_pass = explicit_pages is None and not hit_limit and failed_page is None
        if full_pass:
            log(f"🏁 Full pass for {log_key}: {pages_done} page(s), {collected_pass} properties"
                + (f", realtor.com shows {shown}" if shown is not None else "") + ".")
        else:
            reason = "--pages subset" if explicit_pages is not None else "limit reached" if hit_limit else f"page {failed_page} failed"
            log(f"⏸️ Partial pass for {log_key} ({reason}): {pages_done} page(s), {collected_pass} properties; not marked completed.")
        if log_ref:
            status = {"status": "completed", "last_completed": firestore.SERVER_TIMESTAMP} if full_pass else {"status": "in_progress"}
            await log_ref.set({**status, **counts}, merge=True)

        return True, browser
                
    except Exception as e:
        log(f"❌ Error while scraping {slug}: {e}")
        return True, browser
      
# ==========================================
# 4. ENTRY POINT
# ==========================================

async def main_loop(location_ids, pages, rerun, output=None, limit=None, passes=((None, ""),)):
    """`passes` is a list of (home_type, filter_path): one search per home type, so every
    property is tagged with the type that found it (or a single untyped pass)."""
    locations_mapping = load_locations()
    locations = [loc.strip() for loc in location_ids.split(',')]
    collected: list[dict] = []
    db = None if output else firestore.AsyncClient(project=GCP_PROJECT, database=FIRESTORE_DB)

    log("Starting master nodriver session...")
    browser = await start_browser()

    try:
        for loc_id in locations:
            if limit_reached(collected, limit):
                break

            location_data = locations_mapping.get(loc_id)

            if not location_data:
                log(f"Error: Location ID {loc_id} not found in locations.json.")
                continue

            log(f"\n{'='*40}")
            log(f"🚀 INITIATING SCRAPE FOR: {location_data.get('slug')} (ID: {loc_id})")
            log(f"{'='*40}\n")

            if location_data.get('type') == 'ZipcodeList' and 'zipcodes' in location_data:
                slugs = location_data['zipcodes']
                log(f"Found {len(slugs)} zip codes. Processing sequentially...")
            else:
                slugs = [location_data['slug']]

            # Track if a scrape actually happened for the top-level location
            scraped_location = False

            for slug in slugs:
                for home_type, filter_path in passes:
                    if limit_reached(collected, limit):
                        break

                    # Only pause between searches once we have actually hit the server
                    if scraped_location:
                        log(f"Pausing {T.between_searches}s before the next search...")
                        await asyncio.sleep(T.between_searches)

                    # Capture the return value (and the browser, which may have been restarted)
                    was_scraped, browser = await run_master_scraper(
                        browser, slug, pages, rerun, collected, limit, filter_path=filter_path, home_type=home_type, db=db)
                    scraped_location = scraped_location or was_scraped

            # Apply the same logic to the group delay: only wait if we hit the server
            if loc_id != locations[-1] and scraped_location:
                log(f"\nFinished location group {loc_id}. Pausing {T.between_locations}s before the next group...")
                await asyncio.sleep(T.between_locations)

    finally:
        browser.stop()
        log("\nAll scraping sessions complete. Master browser closed.")
        if output:
            merged = merge_by_property(collected)
            with open(output, "w", encoding="utf-8") as f:
                json.dump(merged, f, indent=2)
            log(f"Wrote {len(merged)} properties to {output}")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Realtor.com JSON Scraper (Nodriver Async)")
    parser.add_argument("--locations", type=str, required=True, help="Comma-separated Location IDs (e.g., '2,32,31')")
    parser.add_argument("--pages", type=str, required=False, default=None, help="Optional: Pages to scrape (e.g., '1, 2-6')")
    parser.add_argument("--rerun", action="store_true", help="Force rerun of previously completed locations")
    parser.add_argument("--output", type=str, required=False, default=None, help="Optional: Write properties to this local JSON file instead of Firestore (Firestore is not touched)")
    parser.add_argument("--limit", type=int, required=False, default=None, help="Optional: Stop after this many properties (a capped pass is never marked completed)")
    parser.add_argument("--home-types", type=str, required=False, default=None, help=f"Optional: Comma-separated home types, searched one at a time and recorded on each property ({', '.join(HOME_TYPES)})")
    parser.add_argument("--listing-type", type=str, required=False, default=None, choices=list(LISTING_TYPES), help="Optional: One listing type (realtor.com accepts only one)")

    args = parser.parse_args()

    home_types = [t.strip() for t in args.home_types.split(',')] if args.home_types else []
    unknown = [t for t in home_types if t not in HOME_TYPES]
    if unknown:
        parser.error(f"unknown --home-types {unknown}; choose from: {', '.join(HOME_TYPES)}")
    # One pass per home type: the type that found a property is its home type
    passes = [(t, build_filter_path([t], args.listing_type)) for t in dict.fromkeys(home_types)] \
        or [(None, build_filter_path([], args.listing_type))]

    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    if args.pages is not None and not parse_page_ranges(args.pages):
        parser.error("--pages has no page numbers")

    try:
        asyncio.run(main_loop(args.locations, args.pages, args.rerun, args.output, args.limit, passes))
    except KeyboardInterrupt:
        log("\nScraper forcefully stopped by user.")