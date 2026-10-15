"""Write-once GCS archive of detail-page captures, keyed by property.

    gs://<bucket>/<prefix>/<property_id>/<captured_at>/page.mhtml      the expanded page
    gs://<bucket>/<prefix>/<property_id>/<captured_at>/property.json   the page's property record

Objects are never overwritten (if_generation_match=0); capturing a property again adds a new
<captured_at> folder next to the old one.
"""
import asyncio

from google.cloud import storage

from settings import ARCHIVE_BUCKET, ARCHIVE_PREFIX, GCP_PROJECT

class Archive:
    def __init__(self, bucket=ARCHIVE_BUCKET, prefix=ARCHIVE_PREFIX):
        self.bucket = storage.Client(project=GCP_PROJECT).bucket(bucket)
        self.prefix = prefix

    def capture_path(self, property_id, captured_at):
        """captured_at is a UTC datetime; the folder name sorts by time."""
        return f"{self.prefix}/{property_id}/{captured_at.strftime('%Y-%m-%dT%H%M%SZ')}"

    def _put(self, name, data, content_type, metadata):
        blob = self.bucket.blob(name)
        blob.metadata = metadata
        blob.upload_from_string(data, content_type=content_type, if_generation_match=0)
        return f"gs://{self.bucket.name}/{name}"

    async def store_capture(self, property_id, captured_at, url, mhtml, record_json):
        """Uploads one capture; returns its gs:// URIs."""
        base = self.capture_path(property_id, captured_at)
        metadata = {"property_id": str(property_id), "url": url}
        mhtml_uri = await asyncio.to_thread(self._put, f"{base}/page.mhtml", mhtml, "multipart/related", metadata)
        json_uri = await asyncio.to_thread(self._put, f"{base}/property.json", record_json, "application/json", metadata)
        return {"mhtml_uri": mhtml_uri, "json_uri": json_uri}
