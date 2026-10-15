import asyncio
import json
import os
import sys
from google.cloud import firestore

# ==========================================
# 1. CONFIGURATION
# ==========================================

# File path to your local JSON master database
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MASTER_DB_FILE = os.path.join(SCRIPT_DIR, "realtor_properties.json")

# Firestore Configuration
GCP_PROJECT = "legalai-460612"
FIRESTORE_DB = "mls-list"
COLLECTION_NAME = "properties"

# ==========================================
# 2. MIGRATION LOGIC
# ==========================================

async def migrate_json_to_firestore():
    print("--- Starting JSON to Firestore Migration ---")
    
    # 1. Load the local JSON file
    if not os.path.exists(MASTER_DB_FILE):
        print(f"❌ Error: Could not find {MASTER_DB_FILE}")
        sys.exit(1)
        
    try:
        with open(MASTER_DB_FILE, 'r', encoding='utf-8') as f:
            properties_list = json.load(f)
    except json.JSONDecodeError:
        print(f"❌ Error: {MASTER_DB_FILE} contains invalid JSON.")
        sys.exit(1)
        
    total_properties = len(properties_list)
    print(f"✅ Loaded {total_properties} properties from local JSON.")
    
    if total_properties == 0:
        print("Nothing to migrate. Exiting.")
        return

    # 2. Initialize the Firestore Async Client
    print(f"🔌 Connecting to Firestore (Project: {GCP_PROJECT}, DB: {FIRESTORE_DB})...")
    db = firestore.AsyncClient(project=GCP_PROJECT, database=FIRESTORE_DB)
    
    # 3. Process in Batches
    print("🚀 Beginning batch uploads...")
    batch = db.batch()
    batch_count = 0
    total_migrated = 0
    
    for prop in properties_list:
        # Determine the primary key (Document ID)
        pid = str(prop.get('property_id') or prop.get('listing_id'))
        
        if not pid or pid == "None":
            continue
            
        # Create a document reference in the target collection
        doc_ref = db.collection(COLLECTION_NAME).document(pid)
        
        # Add the property to the current batch (merge=True ensures we don't accidentally wipe existing fields)
        batch.set(doc_ref, prop, merge=True)
        batch_count += 1
        total_migrated += 1
        
        # Firestore limit: Maximum 500 writes per batch
        if batch_count == 500:
            await batch.commit()
            print(f"   -> Successfully committed batch of 500 (Total migrated: {total_migrated}/{total_properties})")
            
            # Reset batch for the next chunk
            batch = db.batch()
            batch_count = 0
            
    # 4. Commit any remaining properties in the final batch
    if batch_count > 0:
        await batch.commit()
        print(f"   -> Successfully committed final batch of {batch_count} (Total migrated: {total_migrated}/{total_properties})")
        
    print("\n🎉 Migration Complete!")
    print(f"All {total_migrated} properties are now live in Firestore.")

if __name__ == '__main__':
    # Run the async migration
    asyncio.run(migrate_json_to_firestore())