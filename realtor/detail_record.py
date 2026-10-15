"""Reads a detail page's embedded property record and keeps its raw property details.

The page embeds realtor.com's own record for the property in __NEXT_DATA__
(props.pageProps.initialReduxState.propertyDetails), including the "Property details" sections
behind "Show more". Nothing is interpreted here: sections and description are kept exactly as
provided, and standardizing them (types, parcel/APN, ...) is left to post-processing.

No browser or network here, so the same code can re-read an archived property.json.
"""
import json

class DetailPageError(Exception):
    """The page is not the requested property's detail page, or carries no property record."""

def property_record(next_data_text):
    """The propertyDetails record from the page's __NEXT_DATA__ text."""
    try:
        record = json.loads(next_data_text)["props"]["pageProps"]["initialReduxState"]["propertyDetails"]
    except (ValueError, KeyError, TypeError) as e:
        raise DetailPageError(f"no propertyDetails record on the page ({type(e).__name__}: {e})") from e
    if not isinstance(record, dict) or not record.get("property_id"):
        raise DetailPageError("the page's propertyDetails record has no property_id")
    return record

def check_identity(record, property_id, page_path):
    """Raises unless the browser is on this property's detail page."""
    if not page_path.startswith("/realestateandhomes-detail/"):
        raise DetailPageError(f"not a detail page: {page_path}")
    if str(record["property_id"]) != str(property_id):
        raise DetailPageError(f"page shows property {record['property_id']}, expected {property_id}")

def sections(record):
    """The "Property details" sections as provided: parent category, category, lines."""
    return [{"parent_category": s.get("parent_category"), "category": s.get("category"), "text": list(s.get("text") or [])}
            for s in record.get("details") or []]

def detail_fields(record, capture):
    """detail.* for properties/{property_id}; capture holds mhtml_uri, json_uri, captured_at, url."""
    description = record.get("description") or {}
    return {"detail": {**capture, "sections": sections(record), "description": description.get("text")}}
