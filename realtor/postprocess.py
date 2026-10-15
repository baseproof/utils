"""Post-processing: standardized values derived from the raw detail captures.

Reads only what details.py stored (detail.sections in Firestore, property.json in GCS) and never
visits realtor.com. Step 1 is the parcel number:

  1. find the property's jurisdiction from its archived record (county or city, and state) in
     the jurisdiction knowledge file, settings.JURISDICTIONS_FILE (the id.json scrape.py reads)
  2. ask a low-cost model (settings.POSTPROCESS_MODEL) whether a raw "Property details" line
     gives that jurisdiction's property identifier, and which numbered line it is
  3. check the answer: the value must be on that line word for word and match the
     jurisdiction's validationRegex; the standardized form comes from its replaceRegex /
     replacePattern, never from the model; a line carrying one of the jurisdiction's known
     labels that the model passed over is flagged
  4. write parcel.* on properties/{property_id}

parcel.status: standardized | invalid_format | unverified | absent | no_jurisdiction. Only
"standardized" fills parcel.standardized; the others keep the raw value and a reason for review.
"""
import argparse
import asyncio
import collections
import json
import re
import sys
from datetime import datetime, timezone

from google import genai
from google.cloud import firestore, storage
from google.cloud.firestore_v1.base_query import FieldFilter
from google.genai import types
from pydantic import BaseModel

from scrape_utils import log
from settings import FIRESTORE_DB, GCP_PROJECT, JURISDICTIONS_FILE, POSTPROCESS_MODEL, PROPERTIES, VERTEX_LOCATION
from tuning import TUNING

PARCEL_KEYS = ("status", "reason", "raw", "label", "label_known", "line", "section", "standardized",
               "jurisdiction", "model", "processed_at")

class ParcelAnswer(BaseModel):
    present: bool
    line_number: int | None
    label: str | None
    value: str | None

# ==========================================
# 1. KNOWLEDGE AND CHECKS (no network)
# ==========================================
def load_jurisdictions(path=JURISDICTIONS_FILE):
    with open(path, encoding="utf-8") as f:
        return json.load(f)["jurisdictions"]

def value_at(record, path):
    value = record
    for key in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value

def find_jurisdiction(jurisdictions, record):
    """The id.json entry for the record's county or city (exact name and state), or None."""
    state = (value_at(record, "location.county.state_code") or value_at(record, "location.address.state_code") or "").upper()
    names = {"county": value_at(record, "location.county.name"), "city": value_at(record, "location.address.city")}
    for j in jurisdictions:
        loc = j.get("location") or {}
        name = names.get((loc.get("type") or "").lower())
        if name and (loc.get("state") or "").upper() == state and (loc.get("name") or "").casefold() == name.casefold():
            return j
    return None

def raw_lines(sections):
    """(section, line) for every line of detail.sections, in page order."""
    return [(f"{s.get('parent_category')} / {s.get('category')}", line) for s in sections for line in s.get("text") or []]

def js_groups(pattern):
    """id.json replace patterns use JavaScript-style $1 groups."""
    return re.sub(r"\$(\d+)", r"\\\1", pattern)

def known_label_lines(lines, display_names):
    names = {n.strip().rstrip(":").strip().casefold() for n in display_names}
    return [i for i, (_, text) in enumerate(lines) if text.partition(":")[0].strip().casefold() in names]

def build_prompt(jurisdiction, lines):
    loc, ident = jurisdiction["location"], jurisdiction["propertyIdentifier"]
    fmt = ident.get("formatConfig") or {}
    rows = "\n".join(f"{i}. [{section}] {text}" for i, (section, text) in enumerate(lines))
    return (
        f'These numbered lines are the "Property details" of one real-estate listing in {loc["name"]} {loc["type"]}, '
        f"{loc['state']}, exactly as the listing site shows them.\n\n"
        f"Does any line give the property's {ident.get('type', 'parcel')} identifier? Many listings leave it out, so it "
        "may well be missing from these lines; saying it is not present is then the correct answer.\n"
        f"In this jurisdiction it is usually labelled one of: {', '.join(ident.get('displayNames') or [])}. "
        f"Its standard format is {fmt.get('standardFormat')}, and it may be written like: "
        f"{'; '.join(fmt.get('acceptableInputExamples') or [])}. A value in some other shape can still be the "
        "identifier if its line says so; report it as written.\n\n"
        f"{rows}\n\n"
        "Answer with:\n"
        "- present: true only if one of the lines gives that identifier\n"
        "- line_number: the number of that line\n"
        "- label: the label on that line, as written\n"
        "- value: the identifier exactly as written on that line, without reformatting\n"
        "If no line gives it: present false and the other fields null. Do not guess or build one from other lines."
    )

def check_answer(answer, lines, ident):
    """parcel.* fields from the model's answer, checked against the raw lines and the
    jurisdiction's formatConfig."""
    fmt = ident["formatConfig"]
    known = known_label_lines(lines, ident.get("displayNames") or [])
    if not answer.present:
        if known:
            section, line = lines[known[0]]
            return {"status": "unverified", "reason": f"model found no parcel, but line {known[0]} has a known label",
                    "line": line, "section": section}
        return {"status": "absent"}
    if answer.line_number is None or not 0 <= answer.line_number < len(lines):
        return {"status": "unverified", "reason": f"model named line {answer.line_number}, which does not exist"}
    section, line = lines[answer.line_number]
    value = (answer.value or "").strip()
    found = {"raw": value, "label": answer.label, "label_known": answer.line_number in known, "line": line, "section": section}
    if not value or value not in line:
        return {**found, "status": "unverified", "reason": "the value is not on the line the model named"}
    if not re.match(fmt["validationRegex"], value):
        return {**found, "status": "invalid_format", "reason": f"does not match {fmt.get('standardFormat') or fmt['validationRegex']}"}
    standardized = re.sub(fmt["replaceRegex"], js_groups(fmt["replacePattern"]), value)
    return {**found, "status": "standardized", "standardized": standardized}

# ==========================================
# 2. ONE PROPERTY
# ==========================================
def download_json(bucket_client, gs_uri):
    bucket, _, name = gs_uri.removeprefix("gs://").partition("/")
    return json.loads(bucket_client.bucket(bucket).blob(name).download_as_bytes())

async def ask_model(client, prompt):
    response = await client.aio.models.generate_content(
        model=POSTPROCESS_MODEL, contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=ParcelAnswer, temperature=0))
    if not isinstance(response.parsed, ParcelAnswer):
        raise ValueError(f"model answer is not the expected JSON: {(response.text or '')[:200]}")
    return response.parsed

async def process(snap, jurisdictions, client, gcs, semaphore, dry_run):
    async with semaphore:
        detail = snap.to_dict()["detail"]
        record = await asyncio.to_thread(download_json, gcs, detail["json_uri"])
        lines = raw_lines(detail.get("sections") or [])
        jurisdiction = find_jurisdiction(jurisdictions, record)
        parcel: dict[str, object]
        if jurisdiction is None:
            place = value_at(record, "location.county.name") or value_at(record, "location.address.city")
            parcel = {"status": "no_jurisdiction", "reason": f"no id.json entry for {place}, {value_at(record, 'location.county.state_code') or value_at(record, 'location.address.state_code')}"}
        else:
            answer = await ask_model(client, build_prompt(jurisdiction, lines))
            parcel = check_answer(answer, lines, jurisdiction["propertyIdentifier"])
            loc = jurisdiction["location"]
            parcel["jurisdiction"] = {"type": loc.get("type"), "state": loc.get("state"), "name": loc.get("name"), "reference_id": loc.get("referenceId")}
        parcel = {key: parcel.get(key) for key in PARCEL_KEYS} | {"model": POSTPROCESS_MODEL, "processed_at": datetime.now(timezone.utc)}
        if not dry_run:
            await snap.reference.set({"parcel": parcel}, merge=True)
        log(f"   {snap.id}: {parcel['status']:<15} raw={parcel['raw']!r} standardized={parcel['standardized']!r}"
            + (f" ({parcel['reason']})" if parcel["reason"] else ""))
        return snap.id, parcel

# ==========================================
# 3. ORCHESTRATION
# ==========================================
async def build_queue(db, limit, property_ids, reprocess):
    """Captured properties (detail.mhtml_uri set) without a parcel result, or the given ids."""
    col = db.collection(PROPERTIES)
    if property_ids:
        snaps = [await col.document(pid).get() for pid in property_ids]
        missing = [s.id for s in snaps if not s.exists or not ((s.to_dict() or {}).get("detail") or {}).get("json_uri")]
        if missing:
            raise SystemExit(f"❌ not captured yet (no detail.json_uri): {missing}")
    else:
        snaps = [s async for s in col.where(filter=FieldFilter("detail.mhtml_uri", "!=", None)).stream()]
    todo = [s for s in snaps if reprocess or not (s.to_dict().get("parcel") or {}).get("status")]
    return todo[:limit]

async def run(limit, property_ids, reprocess, dry_run):
    jurisdictions = load_jurisdictions()
    db = firestore.AsyncClient(project=GCP_PROJECT, database=FIRESTORE_DB)
    queue = await build_queue(db, limit, property_ids, reprocess)
    if not queue:
        log("✅ No captured properties waiting for post-processing.")
        return 0
    log(f"🚀 Parcel check for {len(queue)} propert{'y' if len(queue) == 1 else 'ies'} with {POSTPROCESS_MODEL}"
        + (" (dry run: nothing written)" if dry_run else "") + "...")
    client = genai.Client(vertexai=True, project=GCP_PROJECT, location=VERTEX_LOCATION)
    gcs = storage.Client(project=GCP_PROJECT)
    semaphore = asyncio.Semaphore(TUNING.postprocess.concurrency)
    results = await asyncio.gather(*(process(s, jurisdictions, client, gcs, semaphore, dry_run) for s in queue))
    counts = collections.Counter(parcel["status"] for _, parcel in results)
    log(f"\n🏁 {dict(counts)}")
    review = [(pid, p["status"], p["reason"]) for pid, p in results if p["status"] != "standardized"]
    for pid, status, reason in review:
        log(f"   review {pid}: {status} {reason or ''}")
    return 0

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Post-process captured detail pages: parcel number check and standardization")
    parser.add_argument("--limit", type=int, default=1000, help="Max properties this run (default 1000)")
    parser.add_argument("--property-ids", type=str, default=None, help="Optional: Comma-separated property ids instead of every unprocessed capture")
    parser.add_argument("--reprocess", action="store_true", help="Process properties that already have a parcel result again")
    parser.add_argument("--dry-run", action="store_true", help="Print the results without writing to Firestore")

    args = parser.parse_args()

    if args.limit < 1:
        parser.error("--limit must be at least 1")
    ids = [p.strip() for p in args.property_ids.split(",") if p.strip()] if args.property_ids else []

    sys.exit(asyncio.run(run(args.limit, ids, args.reprocess, args.dry_run)))
