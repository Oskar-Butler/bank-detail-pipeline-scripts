# ═══════════════════════════════════════════════════════════════
#  Invoice Ingestion — Cloud Function
# ═══════════════════════════════════════════════════════════════
#  Pulls spreadsheets from a Google Drive dropoff folder,
#  deduplicates invoice URLs against Firestore, and uploads
#  new rows as CSV for the OCR worker to process.
#
#  Deployed as an HTTP Cloud Function (Gen1 or Gen2).
# ═══════════════════════════════════════════════════════════════

import os
import io
import re
import json
import hashlib
import datetime
import logging

import pandas as pd
import functions_framework
import google.auth
from google.cloud import storage
from google.cloud import firestore
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from tenacity import retry, stop_after_attempt, wait_exponential

# ── Logging ──────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/cloud-platform",
]

# Characters allowed in sanitised filenames
_SAFE_FILENAME_RE = re.compile(r"[^\w\-.]")


# ═══════════════════════════════════════════════════════════════
#  CONFIGURATION — lazy-loaded at request time, not import time
# ═══════════════════════════════════════════════════════════════

class _Config:
    """Lazily-loaded configuration from environment variables.

    Loading at request time (instead of module import) gives clearer
    error messages in Cloud Functions and makes unit testing easier.
    """

    _instance = None

    def __init__(self):
        self.project_id            = self._require("OCR_PROJECT_ID")
        self.bucket_name           = self._require("OCR_GCS_BUCKET_NAME")
        self.dropoff_folder        = self._require("OCR_DRIVE_DROPOFF_FOLDER_ID")
        self.archive_folder        = self._require("OCR_DRIVE_ARCHIVE_FOLDER_ID")
        self.firestore_collection  = os.environ.get("OCR_FIRESTORE_COLLECTION", "processed_urls")
        self.output_blob           = os.environ.get("OCR_GCS_OUTPUT_BLOB", "ocr_input/current.csv")
        self.max_file_bytes        = int(os.environ.get("OCR_MAX_DRIVE_FILE_BYTES", str(50 * 1024 * 1024)))

    @staticmethod
    def _require(name: str) -> str:
        val = os.environ.get(name)
        if not val:
            raise ValueError(f"Required environment variable {name} is not set")
        return val

    @classmethod
    def get(cls) -> "_Config":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls):
        """Allow tests to force re-initialisation."""
        cls._instance = None


# ═══════════════════════════════════════════════════════════════
#  CLIENT FACTORIES
# ═══════════════════════════════════════════════════════════════

def _get_drive_service():
    creds, _ = google.auth.default(scopes=SCOPES)
    return build("drive", "v3", credentials=creds)


def _get_storage_client(project_id: str):
    return storage.Client(project=project_id)


def _get_firestore_client() -> firestore.Client:
    database_id = os.environ.get("OCR_FIRESTORE_DATABASE_ID", "(default)")
    return firestore.Client(database=database_id)


# ═══════════════════════════════════════════════════════════════
#  GOOGLE DRIVE OPERATIONS
# ═══════════════════════════════════════════════════════════════

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def list_files_in_folder(service, folder_id: str) -> list[dict]:
    """List spreadsheet files (.csv, .xlsx) in the Drive folder."""
    query = (
        f"'{folder_id}' in parents and trashed = false and ("
        "mimeType = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' or "
        "mimeType = 'text/csv'"
        ")"
    )
    results = service.files().list(
        q=query,
        fields="files(id, name, mimeType, size)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    return results.get("files", [])


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def download_file(service, file_id: str) -> io.BytesIO:
    """Download a file from Drive into memory."""
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buf.seek(0)
    return buf


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def move_file_to_archive(service, file_id: str, archive_folder_id: str, filename: str):
    """Move a file to the archive folder with a UTC timestamp suffix."""
    file = service.files().get(fileId=file_id, fields="parents", supportsAllDrives=True).execute()
    previous_parents = ",".join(file.get("parents", []))

    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
    name, ext = os.path.splitext(filename)
    safe_name = _SAFE_FILENAME_RE.sub("_", name)
    new_name = f"{safe_name}_{ts}{ext}"

    service.files().update(
        fileId=file_id,
        addParents=archive_folder_id,
        removeParents=previous_parents,
        body={"name": new_name},
        fields="id, parents, name",
        supportsAllDrives=True,
    ).execute()
    logger.info("Archived %s -> %s", filename, new_name)


# ═══════════════════════════════════════════════════════════════
#  DATA PROCESSING
# ═══════════════════════════════════════════════════════════════

def read_spreadsheet(file_obj: io.BytesIO, filename: str) -> pd.DataFrame:
    """Read a spreadsheet (.csv or .xlsx) into a DataFrame."""
    ext = os.path.splitext(filename)[1].lower()
    
    try:
        if ext == ".csv":
            return pd.read_csv(file_obj)
        elif ext == ".xlsx":
            return pd.read_excel(file_obj, engine="openpyxl")
        else:
            logger.error("Unsupported file extension: %s", ext)
            raise ValueError(f"Unsupported file extension: '{ext}'. Must be .csv or .xlsx")
            
    except Exception as e:
        logger.error("Error reading %s: %s", filename, e)
        raise


# ═══════════════════════════════════════════════════════════════
#  LEDGER — Firestore-backed deduplication
# ═══════════════════════════════════════════════════════════════

# Firestore limits: 500 document refs per get_all(), 500 ops per batch write.
_FIRESTORE_BATCH_LIMIT = 500


def _url_doc_id(url: str) -> str:
    """Deterministic Firestore document ID from a URL.

    SHA-256 avoids special characters (slashes, colons) that are
    illegal in Firestore document IDs, and guarantees uniqueness.
    """
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def check_urls_in_firestore(
    db: firestore.Client,
    collection: str,
    urls: set[str],
) -> set[str]:
    """Return the subset of *urls* that already exist in Firestore.

    Reads are chunked at 500 document refs per get_all() call to
    stay within Firestore payload limits.
    """
    if not urls:
        return set()

    col_ref = db.collection(collection)
    doc_refs = [col_ref.document(_url_doc_id(u)) for u in urls]
    already_processed: set[str] = set()

    # Chunk reads at the Firestore batch limit
    for i in range(0, len(doc_refs), _FIRESTORE_BATCH_LIMIT):
        chunk = doc_refs[i : i + _FIRESTORE_BATCH_LIMIT]
        snapshots = db.get_all(chunk)
        for snap in snapshots:
            if snap.exists:
                already_processed.add(snap.get("url"))

    return already_processed


def persist_urls_to_firestore(
    db: firestore.Client,
    collection: str,
    entries: list[dict],
):
    """Write new URL entries to Firestore in atomic batches.

    Each entry is a dict with keys: url, processed_at, source_file.
    Document ID = sha256(url), so concurrent writes to the same URL
    are idempotent (same doc is overwritten with identical data).

    Writes are chunked at 500 operations per batch (Firestore limit).
    """
    if not entries:
        return

    for i in range(0, len(entries), _FIRESTORE_BATCH_LIMIT):
        chunk = entries[i : i + _FIRESTORE_BATCH_LIMIT]
        batch = db.batch()
        for entry in chunk:
            doc_id = _url_doc_id(entry["url"])
            doc_ref = db.collection(collection).document(doc_id)
            batch.set(doc_ref, {
                "url": entry["url"],
                "processed_at": entry["processed_at"],
                "source_file": entry["source_file"],
            })
        batch.commit()

    logger.info("Persisted %d URL(s) to Firestore collection '%s'", len(entries), collection)


def upload_filtered_data(bucket, df: pd.DataFrame, output_blob: str):
    """Overwrite the input file with new rows."""
    if df.empty:
        logger.info("No new data to upload")
        return

    blob = bucket.blob(output_blob)
    output = io.StringIO()
    df.to_csv(output, index=False)
    blob.upload_from_string(output.getvalue(), content_type="text/csv")
    logger.info("Uploaded %d new rows to gs://%s/%s", len(df), bucket.name, output_blob)


# ═══════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════

@functions_framework.http
def ocr_ingest_invoices(request):
    """HTTP Cloud Function — ingest spreadsheets from Google Drive.

    The `request` parameter is required by functions_framework but is
    intentionally unused: the function always processes every file in
    the configured dropoff folder.

    Returns JSON:  {"status": ..., "new_rows": N, "files_processed": N}
    """
    logger.info("Starting ingestion process")

    def _json_response(payload: dict, status: int = 200):
        return (json.dumps(payload), status, {"Content-Type": "application/json"})

    try:
        cfg = _Config.get()
        drive_service = _get_drive_service()
        storage_client = _get_storage_client(cfg.project_id)
        bucket = storage_client.bucket(cfg.bucket_name)
        db = _get_firestore_client()

        # ── 1. Discover files in the dropoff folder ──────────
        files = list_files_in_folder(drive_service, cfg.dropoff_folder)
        logger.info("Found %d file(s) in dropoff folder", len(files))

        if not files:
            return _json_response({
                "status": "no_files",
                "new_rows": 0,
                "files_processed": 0,
            })

        # ── 2. Process each file, deduplicating via Firestore ─
        processed_urls: set[str] = set()          # running set across files
        new_firestore_entries: list[dict] = []     # entries to persist
        all_new_data: list[pd.DataFrame] = []
        files_to_archive: list[dict] = []

        for file_meta in files:
            file_id  = file_meta["id"]
            filename = file_meta["name"]
            logger.info("Processing: %s", filename)

            # Reject oversized files before downloading
            file_size = int(file_meta.get("size", 0))
            if file_size > cfg.max_file_bytes:
                logger.warning("Skipping %s — too large (%d bytes)", filename, file_size)
                continue

            file_obj = download_file(drive_service, file_id)
            df = read_spreadsheet(file_obj, filename)
            if df is None:
                files_to_archive.append({"id": file_id, "name": filename})
                continue

            # Find the URL/Link column (case-insensitive)
            url_col = None
            for col in df.columns:
                if col.strip().lower() in ("link", "url"):
                    url_col = col
                    break

            if not url_col:
                logger.warning("No 'Link' or 'URL' column in %s — skipping", filename)
                files_to_archive.append({"id": file_id, "name": filename})
                continue

            # Normalise column name to 'Link' for the OCR worker
            if url_col != "Link":
                df = df.rename(columns={url_col: "Link"})
                url_col = "Link"

            # Drop rows with no URL
            na_count = df[url_col].isna().sum()
            if na_count:
                logger.warning("%d rows in %s have no URL — skipped", na_count, filename)
            df = df.dropna(subset=[url_col]).copy()
            current_urls = set(df[url_col].astype(str).tolist())

            # Check Firestore for URLs we haven't already seen in this run
            urls_to_check = current_urls - processed_urls
            if urls_to_check:
                already_known = check_urls_in_firestore(
                    db, cfg.firestore_collection, urls_to_check,
                )
                processed_urls.update(already_known)

            # Filter to genuinely new rows
            is_new = ~df[url_col].astype(str).isin(processed_urls)
            new_rows = df[is_new]

            if not new_rows.empty:
                logger.info("Found %d new rows in %s", len(new_rows), filename)
                all_new_data.append(new_rows)

                # Collect entries for Firestore persistence
                now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
                for url in new_rows[url_col].dropna().astype(str).unique():
                    new_firestore_entries.append({
                        "url": url,
                        "processed_at": now_iso,
                        "source_file": filename,
                    })
                processed_urls.update(
                    new_rows[url_col].dropna().astype(str).tolist()
                )
            else:
                logger.info("No new rows in %s", filename)

            files_to_archive.append({"id": file_id, "name": filename})

        # ── 3. Persist state BEFORE archiving files ──────────
        #  If archiving were done first, a crash here would lose
        #  the files from Drive without recording them in Firestore.
        total_new = 0

        if all_new_data:
            final_output_df = pd.concat(all_new_data, ignore_index=True)
            total_new = len(final_output_df)
            upload_filtered_data(bucket, final_output_df, cfg.output_blob)

            # Persist new URLs to Firestore (idempotent by doc ID)
            if new_firestore_entries:
                persist_urls_to_firestore(
                    db, cfg.firestore_collection, new_firestore_entries,
                )

        # ── 4. Archive processed files (safe — Firestore is updated) ─
        for f in files_to_archive:
            try:
                move_file_to_archive(
                    drive_service, f["id"], cfg.archive_folder, f["name"],
                )
            except Exception as e:
                logger.warning("Failed to archive %s: %s", f["name"], e)

        logger.info("Ingestion complete — %d new rows from %d files", total_new, len(files))
        return _json_response({
            "status": "complete",
            "new_rows": total_new,
            "files_processed": len(files),
        })

    except Exception:
        logger.exception("Ingestion failed with unhandled error")
        return _json_response({"status": "error", "new_rows": 0, "files_processed": 0}, 500)
