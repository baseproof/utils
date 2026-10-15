import asyncio
import json
import random
import os
import sys
import re
import argparse
from datetime import datetime, timezone
import nodriver as uc
from google.cloud import firestore

# ==========================================
# 0. TIMING & DELAY CONSTANTS
# ==========================================
MAX_PROPERTIES_PER_SESSION = 50  # Hard limit to prevent memory leaks/bot bans
DELAY_HESITATION_MIN = 1.0
DELAY_HESITATION_MAX = 2.5
DELAY_SCROLL_MIN = 0.25
DELAY_SCROLL_MAX = 0.75
DELAY_DOM_SETTLE = 1.5
DELAY_BETWEEN_PAGES_MIN = 3.0
DELAY_BETWEEN_PAGES_MAX = 6.0
DELAY_BETWEEN_SESSIONS = 10.0    # Delay between closing browser and starting new session

# ==========================================
# 1. CONFIGURATION & SETUP
# ==========================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ID_CONFIG_FILE = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "common", "id.json"))

# Firestore Configuration
GCP_PROJECT = "legalai-460612"
FIRESTORE_DB = "mls-list"
COLLECTION_NAME = "properties"

def load_json_file(filepath):
    """Safely load a JSON file (used for id.json config)."""
    if not os.path.exists(filepath):
        print(f"❌ Error: {filepath} not found.")
        sys.exit(1)
    with open(filepath, 'r', encoding='utf-8') as f:
        return json.load(f)

def js_to_python_regex_groups(pattern: str) -> str:
    """Converts JavaScript style capture groups ($1) to Python style (\\1)."""
    return re.sub(r'\$(\d+)', r'\\\1', pattern)

# ==========================================
# 2. DOM MANIPULATION & EXTRACTION
# ==========================================
async def smooth_scroll(page):
    """Simulates human scrolling to trigger lazy-loaded elements."""
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

async def expand_all_show_more(page):
    """Bypasses React DOM detachment by waiting for rendering between clicks."""
    print("   🖱️  Expanding hidden 'Show more' sections...")
    js_click_all = """
        async function expandAll() {
            let elements = Array.from(document.querySelectorAll('button, [role="button"], a'));
            for(let el of elements) {
                let text = el.innerText ? el.innerText.toLowerCase() : '';
                if(text.includes('show more') || text.includes('see more')) {
                    el.click();
                    await new Promise(r => setTimeout(r, 400)); // Allow React hydration
                }
            }
        }
        await expandAll();
    """
    await page.evaluate(js_click_all)
    await asyncio.sleep(DELAY_DOM_SETTLE)

def extract_property_id(page_text, raw_html, jurisdiction_config):
    """Extracts and standardizes the ID using the rules defined in id.json."""
    prop_config = jurisdiction_config.get("propertyIdentifier", {})
    display_names = prop_config.get("displayNames", [])
    format_cfg = prop_config.get("formatConfig", {})
    
    val_regex_raw = format_cfg.get("validationRegex", "")
    replace_regex = format_cfg.get("replaceRegex", "")
    
    replace_pattern = js_to_python_regex_groups(format_cfg.get("replacePattern", ""))
    val_regex_inline = val_regex_raw.lstrip("^").rstrip("$")
    clean_html = re.sub(r'<[^>]+>', ' ', raw_html) 
    
    search_targets = [("DOM innerText", page_text), ("Stripped HTML", clean_html)]
    
    for target_name, target_text in search_targets:
        if not target_text: continue
        
        for name in display_names:
            clean_name = name.strip().rstrip(":")
            pattern_str = rf"{re.escape(clean_name)}\s*[:\"]*\s*(?:value[\":\s]*)?({val_regex_inline})"
            pattern = re.compile(pattern_str, re.IGNORECASE)
            
            match = pattern.search(target_text)
            if match:
                raw_id = match.group(1).strip()
                std_id = re.sub(replace_regex, replace_pattern, raw_id)
                return {
                    "type": prop_config.get("type", "unknown"),
                    "matched_label": name,
                    "raw_value": raw_id,
                    "standardized_value": std_id
                }
    return None

# ==========================================
# 3. JURISDICTION & QUEUE MANAGEMENT
# ==========================================
def match_jurisdiction(property_data, id_configs):
    """Matches a property to its specific jurisdiction rules in id.json based on location."""
    loc = property_data.get("location", {})
    address = loc.get("address", {})
    state = address.get("state_code", "").upper()
    
    # Correctly extract county from location.county.name
    county_data = loc.get("county", {})
    county_name = county_data.get("name", "") if isinstance(county_data, dict) else ""
    county_str = county_name.upper().replace(" COUNTY", "").strip()
    
    # Fallback just in case Realtor changes their schema back to putting it in address
    if not county_str:
        county_str = address.get("county", "").upper().replace(" COUNTY", "").strip()
    
    for j in id_configs.get("jurisdictions", []):
        j_loc = j.get("location", {})
        j_state = j_loc.get("state", "").upper()
        j_name = j_loc.get("name", "").upper()
        j_type = j_loc.get("type", "").lower()
        
        if j_state == state:
            if j_type == "county" and j_name in county_str:
                return j
            elif j_type == "city" and j_name in address.get("city", "").upper():
                return j
                
    return None

async def build_scrape_queue(db, id_configs, specific_listing_id=None):
    """Builds a queue of properties from Firestore that need scraping."""
    queue = []
    print("📡 Fetching properties from Firestore to build queue...")
    
    docs = db.collection(COLLECTION_NAME).stream()
    
    async for doc in docs:
        prop = doc.to_dict()
        prop['_doc_id'] = doc.id 
        list_id = str(prop.get('listing_id', ''))
        
        if specific_listing_id:
            if list_id == specific_listing_id or doc.id == specific_listing_id:
                queue.append(prop)
            continue
            
        # --- FAST SKIP: Native Realtor.com New Construction Flag ---
        if prop.get("flags", {}).get("is_new_construction") is True:
            continue
            
        # --- METADATA SKIP: Check if we successfully pulled an ID or previously tagged it ---
        metadata = prop.get("identifier_metadata")
        if isinstance(metadata, dict):
            if metadata.get("standardized_value") or metadata.get("is_new_construction"):
                continue
            
        # --- ATTEMPT SKIP: Check if we already attempted and failed due to timeouts/missing data ---
        if prop.get("identifier_scrape_attempted"):
            continue
            
        if prop.get("full_url") and match_jurisdiction(prop, id_configs):
            queue.append(prop)

    # ==========================================
    # 🚨 SORT BY YEAR BUILT (OLDEST FIRST) 🚨
    # Default to 9999 if 'year_built' is missing so they sink to the bottom
    # ==========================================
    queue.sort(
        key=lambda x: (x.get("description") or {}).get("year_built") or 9999
    )
            
    return queue

# ==========================================
# 4. ORCHESTRATION LOOP
# ==========================================
async def orchestrate_scraping(listing_id=None):
    config_data = load_json_file(ID_CONFIG_FILE)
    db = firestore.AsyncClient(project=GCP_PROJECT, database=FIRESTORE_DB)
    
    session_count = 1
    
    # --- CONTINUOUS SESSION LOOP ---
    while True:
        print(f"\n{'='*60}\n🔍 BUILDING QUEUE FOR SESSION #{session_count}\n{'='*60}")
        queue = await build_scrape_queue(db, config_data, listing_id)
        
        if not queue:
            print("✅ No properties require scraping. Queue is empty. Exiting completely.")
            break

        # --- CONSTANT LIMIT ENFORCEMENT ---
        total_found = len(queue)
        if total_found > MAX_PROPERTIES_PER_SESSION:
            print(f"⚠️ Found {total_found} properties. Capping session at MAX_PROPERTIES_PER_SESSION ({MAX_PROPERTIES_PER_SESSION}).")
            queue = queue[:MAX_PROPERTIES_PER_SESSION]

        print(f"🚀 Starting scraper session #{session_count}. Properties to process: {len(queue)}")
        browser = await uc.start(headless=False, browser_args=["--password-store=basic", "--use-mock-keychain"])
        
        try:
            for idx, prop in enumerate(queue):
                url = prop.get("full_url")
                jurisdiction = match_jurisdiction(prop, config_data)
                year_built = (prop.get("description") or {}).get("year_built") or "Unknown"
                
                doc_id = prop.pop('_doc_id')
                doc_ref = db.collection(COLLECTION_NAME).document(doc_id)
                
                print(f"\n--- [{idx + 1}/{len(queue)}] Processing: {url} (Year Built: {year_built}) ---")

                if not jurisdiction:
                    print("   ⚠️ FAILED: No matching jurisdiction found in id.json for this property's location.")
                    update_payload = {"identifier_scrape_attempted": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")}
                    await doc_ref.set(update_payload, merge=True)
                    continue
                
                try:
                    page = await browser.get(url)
                    await asyncio.sleep(random.uniform(DELAY_HESITATION_MIN, DELAY_HESITATION_MAX))

                    # ==========================================
                    # 🚨 FALLBACK BREADCRUMB CHECK 🚨
                    # Catches "Plan" pages Realtor didn't officially tag in JSON
                    # ==========================================
                    js_breadcrumb = """
                        (() => {
                            let nav = document.querySelector('nav[aria-label="Breadcrumb"]');
                            return nav ? nav.innerText : '';
                        })();
                    """
                    breadcrumb_text = await page.evaluate(js_breadcrumb)
                    
                    if breadcrumb_text and "New Construction" in breadcrumb_text:
                        print(f"   ⏭️  SKIPPING: 'New Construction' detected in page breadcrumbs.")
                        
                        update_payload = {
                            "identifier_metadata": {
                                "is_new_construction": True,
                                "extracted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                            },
                            "identifier_scrape_attempted": firestore.DELETE_FIELD
                        }
                        await doc_ref.set(update_payload, merge=True)
                        
                        if idx < len(queue) - 1:
                            delay = random.uniform(DELAY_BETWEEN_PAGES_MIN, DELAY_BETWEEN_PAGES_MAX)
                            print(f"   ⏳ Waiting {delay:.2f}s before next property...")
                            await asyncio.sleep(delay)
                        continue
                    # ==========================================

                    await smooth_scroll(page)
                    await expand_all_show_more(page)
                    
                    page_text = await page.evaluate("document.body.innerText")
                    raw_html = await page.evaluate("document.documentElement.innerHTML")
                    
                    result = extract_property_id(page_text, raw_html, jurisdiction)
                    update_payload = {}
                    
                    if result:
                        print(f"   🎉 SUCCESS: Found {result['type']} -> {result['standardized_value']}")
                        
                        update_payload["identifier_metadata"] = {
                            "type": result["type"],
                            "matched_label": result["matched_label"],
                            "raw_value": result["raw_value"],
                            "standardized_value": result["standardized_value"],
                            "is_new_construction": False,
                            "extracted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                        }
                        update_payload["identifier_scrape_attempted"] = firestore.DELETE_FIELD
                    else:
                        print("   ⚠️ FAILED: No identifier found. Tagging as attempted.")
                        update_payload["identifier_scrape_attempted"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                        
                    await doc_ref.set(update_payload, merge=True)
                    print("   💾 Successfully updated Firestore record.")
                    
                    if idx < len(queue) - 1:
                        delay = random.uniform(DELAY_BETWEEN_PAGES_MIN, DELAY_BETWEEN_PAGES_MAX)
                        print(f"   ⏳ Waiting {delay:.2f}s before next property...")
                        await asyncio.sleep(delay)
                        
                except Exception as e:
                    print(f"   🚨 Error processing property {doc_id}: {e}")
                    
        finally:
            browser.stop()
            print(f"\n🏁 Session #{session_count} complete. Browser closed.")
            
        if listing_id:
            break
            
        print(f"⏳ Cooling down for {DELAY_BETWEEN_SESSIONS}s before starting next session...")
        await asyncio.sleep(DELAY_BETWEEN_SESSIONS)
        session_count += 1

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Realtor.com ID Scraper (Firestore)")
    parser.add_argument("--listing_id", type=str, required=False, default=None, help="Optional: Scrape a specific listing ID")
    
    args = parser.parse_args()
    
    asyncio.run(orchestrate_scraping(args.listing_id))