"""Where the realtor pipeline keeps its data, shared by every stage.

Firestore properties/{property_id} is the one record per property. Each stage owns its fields:

  inventory (main.py)    card fields (property_id, full_url, url_slug, list_price,
                         description.beds/baths/sqft/lot_sqft, location.address, status, broker),
                         home_types, inventory.first_seen_at / last_seen_at, and the initial
                         detail.mhtml_uri = null that queues the property for the detail process
  details (details.py)   detail.* (mhtml_uri, json_uri, captured_at, url, last_error) plus the
                         raw property details as provided (detail.sections, detail.description)
  post-processing        standardized values and identifiers (parcel/APN, ...) derived from the
  (separate, later)      raw detail fields or the archived property.json

A property is waiting for its detail page while detail.mhtml_uri is null.
"""

GCP_PROJECT = "legalai-460612"
FIRESTORE_DB = "mls-list"
PROPERTIES = "properties"
SCRAPE_LOGS = "scrape_logs"

# Detail-page captures, one prefix per capture:
#   gs://ARCHIVE_BUCKET/ARCHIVE_PREFIX/<property_id>/<captured_at>/page.mhtml
#   gs://ARCHIVE_BUCKET/ARCHIVE_PREFIX/<property_id>/<captured_at>/property.json
ARCHIVE_BUCKET = "legalai-460612-realtor"
ARCHIVE_PREFIX = "realtor"
