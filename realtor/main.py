import asyncio
import json
import random
import os
import sys
import argparse
from datetime import datetime
import nodriver as uc
import nodriver.cdp.page as cdp_page

# Add the Google Cloud Firestore import
from google.cloud import firestore

# ==========================================
# 0. TIMING & DELAY CONSTANTS (Configurable)
# ==========================================

DELAY_PAGE_MIN = 4.0
DELAY_PAGE_MAX = 7.5
DELAY_HESITATION_MIN = 1.0
DELAY_HESITATION_MAX = 2.0
DELAY_SCROLL_MIN = 0.25
DELAY_SCROLL_MAX = 0.75
POLL_INTERVAL = 1.0        
MAX_POLL_RETRIES = 15      
DELAY_DOM_SETTLE = 1.0     
PAGE_TIMEOUT = 180.0       # Max seconds allowed per page scrape

# ==========================================
# 1. CONFIGURATION & SETUP
# ==========================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOCATIONS_FILE = os.path.join(SCRIPT_DIR, "locations.json")
BASE_URL = "https://www.realtor.com/realestateandhomes-search"

def log(msg):
    """Helper to print messages with a timestamp."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}")

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
    pages = set()
    for part in page_string.split(','):
        part = part.strip()
        if '-' in part:
            start, end = map(int, part.split('-'))
            pages.update(range(start, end + 1))
        elif part.isdigit():
            pages.add(int(part))
    return sorted(list(pages))

async def save_mhtml(page, filename="error_page.mhtml"):
    try:
        log(f"--> Saving page state to {filename}...")
        snapshot = await page.send(cdp_page.capture_snapshot(format="mhtml"))
        with open(filename, "w", encoding="utf-8") as f:
            f.write(snapshot.data)
    except Exception as e:
        log(f"--> Failed to save MHTML: {e}. Falling back to standard HTML...")
        html = await page.get_content()
        with open(filename.replace(".mhtml", ".html"), "w", encoding="utf-8") as f:
            f.write(html)

async def smooth_scroll(page):
    js_height_script = "document.body ? document.body.scrollHeight : 0"
    total_height = await page.evaluate(js_height_script)
    current_position = 0
    
    if not isinstance(total_height, (int, float)):
        return
        
    while current_position < total_height:
        scroll_step = random.randint(300, 700)
        current_position += scroll_step
        
        if current_position > total_height:
            current_position = total_height
            
        await page.evaluate(f"window.scrollTo(0, {current_position});")
        await asyncio.sleep(random.uniform(DELAY_SCROLL_MIN, DELAY_SCROLL_MAX))
        
        new_height = await page.evaluate(js_height_script)
        if isinstance(new_height, (int, float)):
            total_height = new_height

async def derive_total_pages(browser, slug):
    """Visits base URL to verify results exist and extracts total pages based ONLY on exact matches."""
    url = f"{BASE_URL}/{slug}"
    log(f"🔍 Checking for results and determining total pages for {slug}...")
    
    page = await browser.get(url)
    
    page_loaded = False
    for _ in range(MAX_POLL_RETRIES):
        if await page.evaluate("!!document.querySelector('#__next') || !!document.querySelector('div[data-testid=\"rdc-property-card\"]')"):
            page_loaded = True
            break
        await asyncio.sleep(POLL_INTERVAL)
        
    if not page_loaded:
        log("⚠️ Bot protection blocked the initial page load. Defaulting to 1 page.")
        return 1
        
    await asyncio.sleep(DELAY_DOM_SETTLE) 
    
    log("Results confirmed! Scrolling to calculate exact match pagination...")
    await smooth_scroll(page)
    await asyncio.sleep(1.0)
    
    js_pagination = """
        (() => {
            const totalEl = document.querySelector('[data-testid="total-results"]');
            if (totalEl && totalEl.innerText.trim() === '0') {
                return 0;
            }
            
            const bodyText = document.body.innerText || "";
            if (bodyText.includes('0 results that match your search')) {
                return 0;
            }
            
            if (totalEl) {
                const totalText = totalEl.innerText.replace(/[^0-9]/g, '');
                const total = parseInt(totalText);
                if (!isNaN(total) && total > 0) {
                    return Math.ceil(total / 42);
                }
            }
            
            const pages = Array.from(document.querySelectorAll('a[aria-label*="page"], a.pagination-item'))
                .map(el => parseInt(el.innerText))
                .filter(n => !isNaN(n))
                .sort((a,b) => b-a);
            return pages.length > 0 ? pages[0] : 1;
        })()
    """
    
    max_page = await page.evaluate(js_pagination)
    
    if max_page == 0:
        log(f"⚠️ 0 exact results found for {slug}. Skipping this location.")
        return 0
        
    log(f"✅ Calculated exact max pages: {max_page}")
    return max_page


# ==========================================
# 3. CORE SCRAPER LOGIC
# ==========================================

async def run_master_scraper(browser, target_slug, pages_str=None, rerun=False):
    """Main orchestration function relying on the globally passed browser."""
    db = firestore.AsyncClient(project="legalai-460612", database="mls-list")
    
    if not target_slug:
        log("⚠️ Invalid target_slug provided. Skipping.")
        return False, browser
        
    slug = str(target_slug)
    log_ref = db.collection("scrape_logs").document(slug)
    
    if not rerun:
        log_doc = await log_ref.get()
        if log_doc.exists and log_doc.to_dict().get("status") == "completed":
            log(f"✅ {slug} previously completed. Skipping to maintain single-pass. (Use --rerun to bypass)")
            return False, browser
            
    await log_ref.set({"status": "in_progress"}, merge=True)
    
    # --- JAVASCRIPT PAYLOAD (Defined once outside loop) ---
    js_extract_script = """
        (() => {
            const properties = [];
            const cards = document.querySelectorAll('div[data-testid="rdc-property-card"]');
            
            cards.forEach(card => {
                try {
                    const isExpanded = card.closest('[data-testid="expanded-property-list"]');
                    if (isExpanded) return; 
                    
                    const wrap = card.closest('[data-property-id]');
                    if (!wrap) return;
                    
                    const property_id = wrap.getAttribute('data-property-id');
                    const listing_id = wrap.getAttribute('data-listing-id');
                    
                    const linkEl = card.querySelector('a[href*="/realestateandhomes-detail/"]');
                    const full_url = linkEl ? "https://www.realtor.com" + linkEl.getAttribute('href').split('?')[0] : "";
                    const url_slug = full_url.split('/').pop() || "";
                    
                    const priceEl = card.querySelector('[data-testid="card-price"]');
                    const list_price = priceEl ? parseInt(priceEl.innerText.replace(/[^0-9]/g, '')) : null;
                    
                    const bedsEl = card.querySelector('[data-testid="property-meta-beds"] .meta-value');
                    const beds = bedsEl ? parseFloat(bedsEl.innerText) : null;
                    
                    const bathsEl = card.querySelector('[data-testid="property-meta-baths"] .meta-value');
                    const baths = bathsEl ? parseFloat(bathsEl.innerText) : null;
                    
                    const sqftEl = card.querySelector('[data-testid="property-meta-sqft"] .meta-value');
                    const sqft = sqftEl ? parseInt(sqftEl.innerText.replace(/[^0-9]/g, '')) : null;
                    
                    const lotEl = card.querySelector('[data-testid="property-meta-lot-size"] .meta-value');
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
                    
                    const addr1El = card.querySelector('[data-testid="card-address-1"]');
                    const addr2El = card.querySelector('[data-testid="card-address-2"]');
                    const line = addr1El ? addr1El.innerText.trim() : "";
                    const addr2 = addr2El ? addr2El.innerText.trim() : "";
                    
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
                    
                    const statusEl = card.querySelector('[data-testid="card-description"] .message');
                    const status = statusEl ? statusEl.innerText.trim() : "";
                    
                    const brokerEl = wrap.querySelector('[data-testid="broker-title"]');
                    const broker = brokerEl ? brokerEl.innerText.replace('Brokered by', '').trim() : "";
                    
                    properties.push({
                        property_id: property_id || null,
                        listing_id: listing_id || null,
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
        if pages_str:
            pages_to_scrape = parse_page_ranges(pages_str)
        else:
            max_p = await derive_total_pages(browser, slug)
            if max_p == 0:
                log(f"⏭️ Skipping {slug} due to no exact results.")
                await log_ref.set({"status": "completed", "last_completed": firestore.SERVER_TIMESTAMP}, merge=True)
                return True, browser
                
            pages_to_scrape = list(range(1, max_p + 1))
            
        log(f"\nTargeting Location: {slug}")
        log(f"Target Pages: {pages_to_scrape}")

        for index, page_num in enumerate(pages_to_scrape):
            
            # Sub-function to isolate per-page logic for timeout enforcement
            async def scrape_single_page(p_num):
                url = f"{BASE_URL}/{slug}/pg-{p_num}"
                log(f"\n--- Scraping Page {p_num} | {url} ---")
                
                page = await browser.get(url)
                
                log("Waiting for page load and bot protection clearance...")
                page_ready = False
                for _ in range(MAX_POLL_RETRIES): 
                    has_data = await page.evaluate("!!document.querySelector('div[data-testid=\"rdc-property-card\"]')")
                    if has_data is True:
                        page_ready = True
                        break
                    await asyncio.sleep(POLL_INTERVAL) 
                
                if not page_ready:
                    log(f"Timeout: Could not find property data. Bot protection likely triggered or page is empty.")
                    await save_mhtml(page, f"failed_bot_challenge_{slug}_pg{p_num}.mhtml")
                    # Force a TimeoutError to trigger the browser restart block below
                    raise asyncio.TimeoutError("Bot protection/empty page detected") 
                
                await asyncio.sleep(random.uniform(DELAY_HESITATION_MIN, DELAY_HESITATION_MAX)) 
                
                log("Scrolling page to trigger lazy loading...")
                await smooth_scroll(page)
                
                log("Extracting payload from DOM...")
                json_payload = await page.evaluate(js_extract_script)
                properties_list = json.loads(json_payload) if json_payload else []
                
                if properties_list:
                    log(f"Extracted {len(properties_list)} exact matches. Checking Firestore for changes...")
                    batch = db.batch()
                    batch_count = 0
                    new_count = 0
                    updated_count = 0
                    unchanged_count = 0
                    new_ids = []
                    updated_ids = []
                    
                    for prop in properties_list:
                        pid = str(prop.get('property_id') or prop.get('listing_id'))
                        if not pid or pid == "None":
                            continue
                        
                        doc_ref = db.collection("properties").document(pid)
                        doc_snapshot = await doc_ref.get()
                        
                        if doc_snapshot.exists:
                            existing_data = doc_snapshot.to_dict()
                            for key, value in existing_data.items():
                                if key not in prop:
                                    prop[key] = value
                                    
                            if existing_data == prop:
                                unchanged_count += 1
                            else:
                                updated_count += 1
                                updated_ids.append(pid)
                                batch.set(doc_ref, prop, merge=True)
                                batch_count += 1
                        else:
                            new_count += 1
                            new_ids.append(pid)
                            batch.set(doc_ref, prop, merge=True)
                            batch_count += 1
                        
                        if batch_count == 500:
                            await batch.commit()
                            batch = db.batch()
                            batch_count = 0

                    if batch_count > 0:
                        await batch.commit()
                        
                    log(f"Page {p_num} Summary: {new_count} NEW, {updated_count} UPDATED, {unchanged_count} UNCHANGED.")
                    if updated_ids:
                        log(f"   -> Updated IDs: {updated_ids}")
                    if new_ids:
                        log(f"   -> New IDs: {new_ids}")
                    
                else:
                    log("No exact matches found on this page. Saving snapshot.")
                    await save_mhtml(page, f"extraction_failed_{slug}_pg{p_num}.mhtml")

            # Execute the page scrape with retry and restart logic
            page_success = False
            for attempt in range(2):
                try:
                    await asyncio.wait_for(scrape_single_page(page_num), timeout=PAGE_TIMEOUT)
                    page_success = True
                    break # Success, break out of retry loop
                except asyncio.TimeoutError:
                    if attempt == 0:
                        log(f"⚠️ LIMIT HIT / TIMEOUT: Page {page_num} exceeded limits. Restarting browser to clear session...")
                        try:
                            browser.stop()
                        except:
                            pass
                        await asyncio.sleep(2) # Give process a moment to close
                        
                        # Spin up fresh instance
                        browser = await uc.start(
                            headless=False,
                            browser_args=[
                                "--password-store=basic", 
                                "--use-mock-keychain"
                            ]
                        )
                        log(f"Browser restarted. Retrying Page {page_num}...")
                    else:
                        log(f"⚠️ TIMEOUT: Page {page_num} failed again after restart. Moving to next page.")
                except Exception as e:
                    log(f"❌ ERROR processing page {page_num}: {e}")
                    break
                
            # Keep the spacing delay outside the timeout logic so we always throttle correctly
            if index < len(pages_to_scrape) - 1 and page_success:
                delay = random.uniform(DELAY_PAGE_MIN, DELAY_PAGE_MAX)
                log(f"Graceful spacing: Waiting {delay:.2f} seconds before loading the next page...")
                await asyncio.sleep(delay)
        
        await log_ref.set({
            "status": "completed",
            "last_completed": firestore.SERVER_TIMESTAMP
        }, merge=True)
        log(f"🏁 Successfully recorded full pass for {slug} in scrape_logs.")
        
        return True, browser
                
    except Exception as e:
        log(f"❌ Error while scraping {slug}: {e}")
        return True, browser
      
# ==========================================
# 4. ENTRY POINT
# ==========================================

async def main_loop(location_ids, pages, rerun):
    locations_mapping = load_locations()
    locations = [loc.strip() for loc in location_ids.split(',')]
    
    log("Starting master nodriver session...")
    browser = await uc.start(
        headless=False,
        browser_args=[
            "--password-store=basic", 
            "--use-mock-keychain"
        ]
    )
    
    try:
        for loc_id in locations:
            location_data = locations_mapping.get(loc_id)
            
            if not location_data:
                log(f"Error: Location ID {loc_id} not found in locations.json.")
                continue
                
            log(f"\n{'='*40}")
            log(f"🚀 INITIATING SCRAPE FOR: {location_data.get('slug')} (ID: {loc_id})")
            log(f"{'='*40}\n")
            
            # Track if a scrape actually happened for the top-level location
            scraped_location = False 

            if location_data.get('type') == 'ZipcodeList' and 'zipcodes' in location_data:
                zip_list = location_data['zipcodes']
                log(f"Found {len(zip_list)} zip codes. Processing sequentially...")
                
                for idx, zipcode in enumerate(zip_list):
                    log(f"\n>>> Target Zipcode: {zipcode} <<<")
                    
                    # Capture the return value
                    was_scraped = await run_master_scraper(browser, zipcode, pages, rerun)
                    
                    # Only pause if we actually scraped something
                    if was_scraped and idx < len(zip_list) - 1:
                        log("Pausing 5s before next ZIP code...")
                        await asyncio.sleep(5)
                        scraped_location = True # Mark that we hit the server at least once
            
            else:
                scraped_location = await run_master_scraper(browser, location_data['slug'], pages, rerun)
            
            # Apply the same logic to the group delay: only wait if we hit the server
            if loc_id != locations[-1] and scraped_location:
                log(f"\nFinished location group {loc_id}. Pausing 10s before the next group...")
                await asyncio.sleep(10)
                
    finally:
        browser.stop()
        log("\nAll scraping sessions complete. Master browser closed.")
        
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Realtor.com JSON Scraper (Nodriver Async)")
    parser.add_argument("--locations", type=str, required=True, help="Comma-separated Location IDs (e.g., '2,32,31')")
    parser.add_argument("--pages", type=str, required=False, default=None, help="Optional: Pages to scrape (e.g., '1, 2-6')")
    parser.add_argument("--rerun", action="store_true", help="Force rerun of previously completed locations")
    
    args = parser.parse_args()
    
    try:
        asyncio.run(main_loop(args.locations, args.pages, args.rerun))
    except KeyboardInterrupt:
        log("\nScraper forcefully stopped by user.")