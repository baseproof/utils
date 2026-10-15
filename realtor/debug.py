import asyncio
import argparse
import json
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

# ==========================================
# CONFIGURATION
# ==========================================
GCP_PROJECT = "legalai-460612"
FIRESTORE_DB = "mls-list"
COLLECTION_NAME = "properties"

async def fetch_and_print_property(doc_id):
    print(f"📡 Connecting to Firestore (Project: {GCP_PROJECT}, DB: {FIRESTORE_DB})...")
    
    db = firestore.AsyncClient(project=GCP_PROJECT, database=FIRESTORE_DB)
    doc_ref = db.collection(COLLECTION_NAME).document(doc_id)
    
    print(f"🔍 Searching for document ID: '{doc_id}'...")
    doc = await doc_ref.get()
    
    if doc.exists:
        print("\n✅ Document found! Here is the full JSON payload:\n")
        print("=" * 60)
        data = doc.to_dict()
        print(json.dumps(data, indent=4, default=str))
        print("=" * 60)
    else:
        print(f"\n❌ Error: No document found in the '{COLLECTION_NAME}' collection with ID '{doc_id}'.")

async def count_new_construction():
    print(f"📡 Connecting to Firestore (Project: {GCP_PROJECT}, DB: {FIRESTORE_DB})...")
    db = firestore.AsyncClient(project=GCP_PROJECT, database=FIRESTORE_DB)
    
    print("🔍 Calculating total 'New Construction' properties...")
    
    # Target the nested field "flags.is_new_construction" == True
    query = db.collection(COLLECTION_NAME).where(
        filter=FieldFilter("flags.is_new_construction", "==", True)
    )
    
    # Use Firestore's server-side count() for efficiency
    aggregate_query = query.count()
    results = await aggregate_query.get()
    
    # Extract the count value from the result payload
    total_count = results[0][0].value
    
    print("=" * 60)
    print(f"🏗️  Total New Construction Listings: {total_count}")
    print("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Firestore Debugger for MLS properties")
    
    parser.add_argument("--id", type=str, help="The property_id or listing_id to fetch (e.g., 9664908350)")
    parser.add_argument("--count-new", action="store_true", help="Get the total count of new construction listings")
    
    args = parser.parse_args()
    
    if args.count_new:
        asyncio.run(count_new_construction())
    elif args.id:
        asyncio.run(fetch_and_print_property(args.id))
    else:
        print("❌ Please provide an argument.")
        print("Usage examples:")
        print("  python debug.py --id 9664908350")
        print("  python debug.py --count-new")