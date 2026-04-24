# ══════════════════════════════════════════════════════════════
#  Bank OCR — Cloud Run Worker
#  Extracts structured invoice data via Gemini Pro Vision
# ══════════════════════════════════════════════════════════════

import logging
import os
import io
import json
import time
import random
import re
import csv
import sys
import signal
import asyncio
import threading
import traceback
import ipaddress
import socket
from urllib.parse import urlparse

import certifi
import urllib3
import pandas as pd
from pypdf import PdfReader
from PIL import Image
from google.cloud import storage
from google import genai
from google.genai import types
from google.api_core.exceptions import ResourceExhausted, ServiceUnavailable

# Suppress verbose library logs
logging.getLogger("pypdf._reader").setLevel(logging.ERROR)


# ══════════════════════════════════════════════════════════════
#  STRUCTURED LOGGING — Cloud Logging compatible JSON
# ══════════════════════════════════════════════════════════════

class _JsonFormatter(logging.Formatter):
    """Emits properly-escaped JSON lines for Cloud Logging.

    Unlike a bare f-string template, json.dumps guarantees that
    quotes and special characters in the message are escaped.
    """

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S"),
            "severity": record.levelname,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0]:
            entry["stack_trace"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


logger = logging.getLogger("bank_ocr")
logger.setLevel(logging.INFO)
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(_JsonFormatter())
logger.addHandler(_handler)

logger.info("Imports successful")


# ══════════════════════════════════════════════════════════════
#  CONFIGURATION — all values from environment variables
# ══════════════════════════════════════════════════════════════

# Core identifiers (validated at startup)
PROJECT_ID = os.environ.get("OCR_PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT")

# GCS paths — no hardcoded bucket names; validated at startup
INPUT_GCS_PATH = (
    os.environ.get("OCR_INPUT_GCS_PATH")
    or os.environ.get("OCR_EXCEL_GCS_PATH")
)
EXPORT_BUCKET_NAME = os.environ.get("OCR_EXPORT_BUCKET_NAME")
EXPORT_PREFIX = os.environ.get("OCR_EXPORT_PREFIX", "ocr_output")
FILE_OVERWRITE_NAME = os.environ.get("OCR_FILE_OVERWRITE_NAME", "")
LOCATION = os.environ.get("OCR_LOCATION", "us-central1")

# Sharding for horizontal scaling
SHARD_COUNT = int(os.environ.get("OCR_SHARD_COUNT", "5"))
CLOUD_RUN_TASK_INDEX = int(os.environ.get("CLOUD_RUN_TASK_INDEX", "0"))
SHARD_INDEX = CLOUD_RUN_TASK_INDEX + 1

# Gemini configuration
GEMINI_MODEL = os.environ.get("OCR_GEMINI_MODEL", "gemini-2.5-pro")
MAX_PDF_PAGES = int(os.environ.get("OCR_MAX_PDF_PAGES", "10"))
MIN_IMAGE_WIDTH = int(os.environ.get("OCR_MIN_IMAGE_WIDTH", "400"))
MIN_IMAGE_HEIGHT = int(os.environ.get("OCR_MIN_IMAGE_HEIGHT", "400"))
ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".tiff"}

GEMINI_RETRIES = int(os.environ.get("OCR_GEMINI_RETRIES", "6"))
GEMINI_DELAY = float(os.environ.get("OCR_GEMINI_DELAY", "5.0"))
GEMINI_CONCURRENT_LIMIT = int(os.environ.get("OCR_GEMINI_CONCURRENT_LIMIT", "2"))
GEMINI_RPM_LIMIT = int(os.environ.get("OCR_GEMINI_RPM_LIMIT", "12"))
GEMINI_TIMEOUT = float(os.environ.get("OCR_GEMINI_TIMEOUT", "120.0"))

# Size limits
MAX_INPUT_SIZE = int(os.environ.get("OCR_MAX_INPUT_SIZE",
    os.environ.get("OCR_MAX_EXCEL_SIZE", str(50 * 1024 * 1024))))    # 50 MB
MAX_INPUT_ROWS = int(os.environ.get("OCR_MAX_INPUT_ROWS",
    os.environ.get("OCR_MAX_EXCEL_ROWS", "10000")))
MAX_DOWNLOAD_SIZE = int(os.environ.get("OCR_MAX_DOWNLOAD_SIZE",
    str(100 * 1024 * 1024)))                                          # 100 MB

# SSRF protection — blocked metadata endpoints
BLOCKED_HOSTS = frozenset({
    "metadata.google.internal",
    "169.254.169.254",
    "metadata.goog",
})


# ══════════════════════════════════════════════════════════════
#  FIELD DEFINITIONS — single source of truth
# ══════════════════════════════════════════════════════════════

BANKING_FIELDS = [
    "Account_Name", "Bank_Name", "Account_Number",
    "Sort_Code", "Routing_Number", "Transit_Number",
    "Branch_Code", "SWIFT_BIC", "IBAN",
]

INVOICE_FIELDS = [
    "Invoice_Number", "Invoice_Date", "Due_Date", "Currency",
    "Subtotal", "Tax_Amount", "Tax_Rate", "Total_Amount",
    "Document_Type", "Credit_Note_Reference", "Payment_Terms",
    "Job_Number", "Accounting_Description",
]

VENDOR_FIELDS = ["Vendor_Name", "Vendor_TAX_Number", "Vendor_Address"]

ALL_EXTRACTION_FIELDS = BANKING_FIELDS + INVOICE_FIELDS + VENDOR_FIELDS

# Fields sourced directly from the input spreadsheet (never extracted by AI)
PASSTHROUGH_FIELDS = [
    "Office_Code", "Office_Name",
    "Master_Supplier_Code", "Master_Supplier_Name",
    "Supplier_Code", "Supplier_Name",
    "Invoice_No", "Expected_Job_Number",
]


# ══════════════════════════════════════════════════════════════
#  EXTRACTION PROMPT
# ══════════════════════════════════════════════════════════════
# Note: the example IBAN below (GB29NWBK60161331926819) is the
# widely-published test IBAN used in IBAN documentation.

EXTRACTION_PROMPT = """You are an expert financial data extractor. Analyze this invoice image/document and extract all relevant banking and invoice details.

Extract the following fields (return empty string "" if not found):

**Banking Details:**
- `Account_Name` - Full legal name on the bank account
- `Bank_Name` - Name of the bank
- `Account_Number` - Bank account number (see formatting rules below)
- `Sort_Code` - UK sort code, 6 digits (see formatting rules below)
- `Routing_Number` - US ABA routing number, 9 digits (see formatting rules below)
- `Transit_Number` - Canadian transit/branch number, 5 digits (see formatting rules below)
- `Branch_Code` - South African branch or universal branch code, 6 digits (see formatting rules below)
- `SWIFT_BIC` - Bank Identifier Code / SWIFT code (8 or 11 uppercase characters)
- `IBAN` - International Bank Account Number (starts with 2-letter country code, uppercase)

**CRITICAL — Numeric Banking Field Formatting Rules:**
For the fields Account_Number, Sort_Code, Routing_Number, Transit_Number, and Branch_Code:
1. Strip ALL hyphens and spaces from the raw value.
2. Prepend a single apostrophe character (') so that spreadsheet software preserves leading zeros.
3. If the field is not present on the invoice, return an empty string "".
Examples:
  - Sort code "20-00-00"  → "'200000"
  - Account number "01234567" → "'01234567"
  - Routing number "021-000-021" → "'021000021"
  - Transit number "00240" → "'00240"
  - Branch code "198765" → "'198765"
SWIFT_BIC and IBAN do not need an apostrophe prefix.

**Invoice Details:**
- `Invoice_Number` - Invoice reference number
- `Invoice_Date` - Date of invoice (UK format: DD/MM/YYYY)
- `Due_Date` - Payment due date (UK format: DD/MM/YYYY)
- `Currency` - 3-letter ISO currency code (e.g. GBP, USD, ZAR, CAD; default to GBP if unclear)
- `Subtotal` - Amount before tax (numeric only, no currency symbols or commas)
- `Tax_Amount` - Tax/VAT amount (numeric only, no currency symbols or commas)
- `Tax_Rate` - Tax/VAT rate percentage (e.g. "20" for 20%)
- `Total_Amount` - Total invoice amount including tax (numeric only, no currency symbols or commas)
- `Document_Type` - Type of document: must be exactly one of "Invoice", "Credit Note", "Pro Forma", "Statement", or "Remittance Advice". Default to "Invoice" if unclear.
- `Credit_Note_Reference` - If Document_Type is "Credit Note", the original invoice number this credit note relates to. Otherwise empty string.
- `Payment_Terms` - Payment terms as stated on the document (e.g. "30 days net", "14 days from invoice date", "immediate")
- `Job_Number` - The client's internal job or project reference number printed on the invoice. These follow a specific format: 2-4 uppercase client letters + 3-4 uppercase office letters + 3-6 digits, optionally followed by a slash and revision digits. Examples: ACMLDN0537, GLXMOD0005, GLXMOD0005/03, INTLDN0014, ACMLDN0384, GLXSTU0003, INTLDN0002. Extract the first match found. Return empty string if none found.
- `Accounting_Description` - A structured description for the general ledger, formatted EXACTLY as:
  "[Project Name] - [Service/Deliverable] - [Billing Phase/Percentage]"
  Rules:
  • Project Name: the campaign or project name found on the invoice.
  • Service/Deliverable: the specific service or goods being billed (use line item descriptions if visible).
  • Billing Phase/Percentage: the instalment, milestone, or percentage if stated; otherwise use "Full Amount".
  • Keep the total under 120 characters.
  Examples:
  - "Globex Retail Spring Campaign - Film Production Services - 40% Instalment"
  - "Acme Foods Summer 2024 - VFX & Post-Production - Full Amount"
  - "Initech Home 2024 - Photography & Retouching - 50% Deposit"

**Vendor Details:**
- `Vendor_Name` - Name of the supplier/vendor
- `Vendor_TAX_Number` - Tax registration number (VAT, GST, EIN, etc.)
- `Vendor_Address` - Full address of the vendor

**Rules:**
- Only extract what is explicitly visible. Do not guess or infer.
- For amounts, return only the numeric value without currency symbols or commas.
- For dates, use UK format DD/MM/YYYY (e.g., 15/01/2024 for 15th January 2024).
- Do NOT extract or include: Office_Code, Office_Name, Master_Supplier_Code, Master_Supplier_Name, Supplier_Code, Supplier_Name, Invoice_No, Expected_Job_Number — these are sourced from elsewhere and must be omitted.
- Return a single valid JSON object with exactly the fields listed above, no others.

**Example Output:**
{
  "Account_Name": "ABC Ltd",
  "Bank_Name": "NatWest",
  "Account_Number": "'12345678",
  "Sort_Code": "'200000",
  "Routing_Number": "",
  "Transit_Number": "",
  "Branch_Code": "",
  "SWIFT_BIC": "NWBKGB2L",
  "IBAN": "GB29NWBK60161331926819",
  "Invoice_Number": "INV-2024-001",
  "Invoice_Date": "15/01/2024",
  "Due_Date": "15/02/2024",
  "Currency": "GBP",
  "Subtotal": "1000.00",
  "Tax_Amount": "200.00",
  "Tax_Rate": "20",
  "Total_Amount": "1200.00",
  "Document_Type": "Invoice",
  "Credit_Note_Reference": "",
  "Payment_Terms": "30 days net",
  "Job_Number": "ACMLDN0537",
  "Accounting_Description": "Acme Foods Summer 2024 - VFX & Post-Production - Full Amount",
  "Vendor_Name": "ABC Supplies Ltd",
  "Vendor_TAX_Number": "GB123456789",
  "Vendor_Address": "123 High Street, London, EC1A 1AA"
}

Analyze the document and return ONLY the JSON object, no other text."""


# ══════════════════════════════════════════════════════════════
#  GRACEFUL SHUTDOWN
# ══════════════════════════════════════════════════════════════

_shutdown_event = threading.Event()


def _handle_sigterm(signum, frame):
    logger.warning("SIGTERM received — will export partial results after current batch")
    _shutdown_event.set()


signal.signal(signal.SIGTERM, _handle_sigterm)


# ══════════════════════════════════════════════════════════════
#  CONFIG VALIDATION
# ══════════════════════════════════════════════════════════════

def validate_config():
    """Validate all configuration at startup. Exits with code 2 on failure."""
    errors = []

    if not PROJECT_ID:
        errors.append("PROJECT_ID environment variable is required")
    if not INPUT_GCS_PATH:
        errors.append("INPUT_GCS_PATH environment variable is required")
    if not EXPORT_BUCKET_NAME:
        errors.append("EXPORT_BUCKET_NAME environment variable is required")
    if SHARD_COUNT <= 0:
        errors.append(f"SHARD_COUNT must be positive, got {SHARD_COUNT}")
    if GEMINI_RPM_LIMIT <= 0:
        errors.append(f"GEMINI_RPM_LIMIT must be positive, got {GEMINI_RPM_LIMIT}")
    if not (0 <= CLOUD_RUN_TASK_INDEX < SHARD_COUNT):
        errors.append(
            f"CLOUD_RUN_TASK_INDEX {CLOUD_RUN_TASK_INDEX} out of range "
            f"for SHARD_COUNT {SHARD_COUNT}"
        )
    if MAX_PDF_PAGES <= 0:
        errors.append(f"MAX_PDF_PAGES must be positive, got {MAX_PDF_PAGES}")
    if GEMINI_TIMEOUT <= 0:
        errors.append(f"GEMINI_TIMEOUT must be positive, got {GEMINI_TIMEOUT}")

    if errors:
        for err in errors:
            logger.error("Config error: %s", err)
        sys.exit(2)


# ══════════════════════════════════════════════════════════════
#  URL SECURITY & VALIDATION
# ══════════════════════════════════════════════════════════════

def parse_gcs_url(url: str) -> tuple[str, str]:
    parts = url.replace("gs://", "", 1).split("/", 1)
    if len(parts) != 2:
        raise ValueError(f"GCS URL missing blob path: {url}")
    return parts[0], parts[1]


def get_mime_type(ext: str) -> str:
    """Get MIME type from file extension."""
    mime_map = {
        "pdf": "application/pdf",
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "tiff": "image/tiff",
    }
    return mime_map.get(ext.lower().lstrip("."), "application/octet-stream")


def get_url_extension(url: str) -> str:
    """Safely extract file extension from a URL, ignoring query params."""
    parsed = urlparse(url)
    _, ext = os.path.splitext(parsed.path)
    return ext.lower()


def is_safe_url(url: str) -> tuple[bool, str, str | None]:
    """SSRF protection: hostname blocklist + DNS resolution + private IP check.

    Returns (safe, reason, resolved_ip).  The resolved IP should be
    passed to _download_file to prevent DNS rebinding attacks (TOCTOU).
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()

    if not host:
        return False, "URL has no hostname", None

    # Explicit hostname blocklist (defense-in-depth)
    if host in BLOCKED_HOSTS:
        return False, f"Blocked metadata endpoint: {host}", None

    if parsed.scheme == "http":
        return False, "HTTP not allowed, use HTTPS or gs://", None

    # Resolve DNS and validate every returned address
    try:
        addr_infos = socket.getaddrinfo(host, parsed.port or 443)
    except socket.gaierror:
        return False, f"Cannot resolve hostname: {host}", None

    if not addr_infos:
        return False, f"DNS returned no addresses for: {host}", None

    for addr_info in addr_infos:
        ip_str = addr_info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast):
            return False, f"Blocked: {host} resolves to reserved/private IP {ip}", None

    # Return first resolved IP for DNS pinning
    resolved_ip = addr_infos[0][4][0]
    return True, "OK", resolved_ip


def should_process_file(url: str) -> tuple[bool, str, str | None]:
    """Lightweight pre-flight validation (URL format, extension, SSRF).

    Returns (ok, reason, resolved_ip).  The resolved IP is non-None
    for HTTPS URLs and should be threaded through to the download step.
    """
    if not isinstance(url, str) or not url.strip():
        return False, "Invalid URL (empty or not a string)", None

    resolved_ip = None

    if url.lower().startswith("gs://"):
        try:
            parse_gcs_url(url)
        except ValueError as e:
            return False, str(e), None
    elif url.lower().startswith("http"):
        safe, reason, resolved_ip = is_safe_url(url)
        if not safe:
            return False, f"URL rejected: {reason}", None
    else:
        return False, f"Unsupported URL scheme: {url}", None

    ext = get_url_extension(url)
    if not ext or ext not in ALLOWED_EXTENSIONS:
        return False, f"File type not allowed: {ext or '(none)'}", None

    return True, "File approved", resolved_ip


# ══════════════════════════════════════════════════════════════
#  FILE DOWNLOAD — with DNS pinning for HTTPS
# ══════════════════════════════════════════════════════════════

def _download_https_pinned(url: str, resolved_ip: str) -> bytes:
    """Download a file over HTTPS, connecting to a pre-resolved IP.

    This prevents DNS rebinding (TOCTOU) attacks: the TCP connection
    goes to the exact IP that was validated during the safety check.
    TLS certificate verification still uses the original hostname via SNI.
    """
    parsed = urlparse(url)
    hostname = parsed.hostname
    port = parsed.port or 443
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    pool = urllib3.HTTPSConnectionPool(
        host=resolved_ip,
        port=port,
        server_hostname=hostname,
        cert_reqs="CERT_REQUIRED",
        ca_certs=certifi.where(),
        timeout=urllib3.Timeout(connect=10.0, read=30.0),
        maxsize=1,
    )

    try:
        resp = pool.request(
            "GET",
            path,
            headers={
                "Host": hostname,
                "User-Agent": "bank-ocr-worker/1.0",
            },
            redirect=False,
            preload_content=False,
        )

        if resp.status != 200:
            raise ValueError(f"HTTP {resp.status} from {hostname}{path}")

        chunks: list[bytes] = []
        total = 0
        for chunk in resp.stream(65_536):
            total += len(chunk)
            if total > MAX_DOWNLOAD_SIZE:
                raise ValueError(
                    f"Download exceeded size limit ({MAX_DOWNLOAD_SIZE} bytes): {url}"
                )
            chunks.append(chunk)

        return b"".join(chunks)
    finally:
        pool.close()


def _download_file(
    url: str,
    storage_client: storage.Client,
    resolved_ip: str | None = None,
) -> bytes:
    """Download a file from GCS or HTTPS.

    For HTTPS URLs, `resolved_ip` is used for DNS-pinned connections.
    For GCS URLs, the storage client handles the download directly.
    """
    if url.lower().startswith("gs://"):
        bucket_name, blob_name = parse_gcs_url(url)
        blob = storage_client.bucket(bucket_name).blob(blob_name)
        blob.reload()
        if blob.size and blob.size > MAX_DOWNLOAD_SIZE:
            raise ValueError(
                f"GCS file too large: {blob.size} bytes (max {MAX_DOWNLOAD_SIZE})"
            )
        return blob.download_as_bytes()

    if not resolved_ip:
        raise ValueError(f"HTTPS download requires a resolved IP: {url}")

    return _download_https_pinned(url, resolved_ip)


# ══════════════════════════════════════════════════════════════
#  FILE CONTENT VALIDATION
# ══════════════════════════════════════════════════════════════

def validate_file_content(file_bytes: bytes, ext: str) -> tuple[bool, str]:
    """Validate downloaded file content (page count, image dimensions).

    Handles corrupt or malformed files gracefully instead of raising.
    """
    try:
        if ext == ".pdf":
            reader = PdfReader(io.BytesIO(file_bytes))
            if len(reader.pages) > MAX_PDF_PAGES:
                return False, f"PDF too long ({len(reader.pages)} pages, max {MAX_PDF_PAGES})"
        elif ext in {".png", ".jpg", ".jpeg", ".tiff"}:
            with Image.open(io.BytesIO(file_bytes)) as img:
                w, h = img.size
                if w < MIN_IMAGE_WIDTH or h < MIN_IMAGE_HEIGHT:
                    return False, f"Image too small ({w}x{h}, min {MIN_IMAGE_WIDTH}x{MIN_IMAGE_HEIGHT})"
    except Exception as e:
        return False, f"File content validation failed: {e}"

    return True, "OK"


# ══════════════════════════════════════════════════════════════
#  JSON PARSING
# ══════════════════════════════════════════════════════════════

def get_empty_result() -> dict:
    """Return an empty result dict with all extraction fields."""
    return {f: "" for f in ALL_EXTRACTION_FIELDS}


def parse_gemini_json(text: str) -> dict:
    """Parse JSON from Gemini response, stripping markdown fences if present."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text.strip())
    return json.loads(text)


# ══════════════════════════════════════════════════════════════
#  RATE LIMITER — leaky bucket with burst support
# ══════════════════════════════════════════════════════════════

class RateLimiter:
    """Async leaky-bucket rate limiter supporting short bursts."""

    def __init__(self, rpm: int, burst: int = 1):
        self.interval = 60.0 / rpm
        self.tokens = float(burst)
        self.max_tokens = float(burst)
        self.last_refill = time.time()
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = time.time()
            elapsed = now - self.last_refill
            self.tokens = min(self.max_tokens, self.tokens + elapsed / self.interval)
            self.last_refill = now
            if self.tokens < 1.0:
                wait = (1.0 - self.tokens) * self.interval
                await asyncio.sleep(wait)
                self.tokens = 0.0
            else:
                self.tokens -= 1.0


# ══════════════════════════════════════════════════════════════
#  GEMINI EXTRACTION
# ══════════════════════════════════════════════════════════════

async def extract_with_gemini_async(
    file_bytes: bytes,
    file_ext: str,
    gemini_client: genai.Client,
    semaphore: asyncio.Semaphore,
    rate_limiter: RateLimiter,
    row_info: dict,
) -> dict:
    """Extract invoice data using Gemini vision capabilities asynchronously."""
    async with semaphore:
        await rate_limiter.acquire()
        mime_type = get_mime_type(file_ext)

        for attempt in range(1, GEMINI_RETRIES + 1):
            try:
                parts = [
                    types.Part.from_bytes(data=file_bytes, mime_type=mime_type),
                    EXTRACTION_PROMPT,
                ]

                response = await asyncio.wait_for(
                    gemini_client.aio.models.generate_content(
                        model=GEMINI_MODEL,
                        contents=parts,
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            temperature=0.1,
                        ),
                    ),
                    timeout=GEMINI_TIMEOUT,
                )

                if not response.text:
                    raise ValueError("Empty response from Gemini")

                result = parse_gemini_json(response.text)
                logger.info("Row %s: Gemini extraction successful", row_info.get("row"))
                return result

            # Retryable transient errors
            except (ResourceExhausted, ServiceUnavailable, asyncio.TimeoutError) as e:
                wait = min(120.0, GEMINI_DELAY * (2 ** (attempt - 1))) + random.uniform(0, 5)
                logger.warning(
                    "Row %s: Retryable error on attempt %d (retry in %.1fs): %s",
                    row_info.get("row"), attempt, wait, e,
                )
                if attempt < GEMINI_RETRIES:
                    await asyncio.sleep(wait)
                else:
                    logger.error("Row %s: Gemini failed after %d attempts", row_info.get("row"), attempt)
                    return get_empty_result()

            # Non-retryable parse errors
            except (json.JSONDecodeError, ValueError) as e:
                logger.error(
                    "Row %s: Non-retryable parse error: %s", row_info.get("row"), e
                )
                return get_empty_result()

            # Catch-all: inspect error message for retryable patterns
            except Exception as e:
                err_str = str(e)
                is_retryable = any(s in err_str for s in ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE"))

                if is_retryable and attempt < GEMINI_RETRIES:
                    wait = min(120.0, GEMINI_DELAY * (2 ** (attempt - 1))) + random.uniform(0, 5)
                    logger.warning(
                        "Row %s: Transient error on attempt %d (retry in %.1fs): %s",
                        row_info.get("row"), attempt, wait, e,
                    )
                    await asyncio.sleep(wait)
                elif attempt < GEMINI_RETRIES:
                    wait = GEMINI_DELAY + random.uniform(0, 2)
                    logger.warning(
                        "Row %s: Unexpected error on attempt %d (retry in %.1fs): %s",
                        row_info.get("row"), attempt, wait, e,
                    )
                    await asyncio.sleep(wait)
                else:
                    logger.error("Row %s: Gemini failed after %d attempts", row_info.get("row"), attempt)
                    return get_empty_result()

    return get_empty_result()


# ══════════════════════════════════════════════════════════════
#  ASYNC PROCESSING ORCHESTRATOR
# ══════════════════════════════════════════════════════════════

async def process_all_invoices_async(
    approved_refs: list[tuple[int, str, str | None]],
    gemini_client: genai.Client,
    storage_client: storage.Client,
) -> list[dict]:
    """Process all invoices concurrently with rate limiting.

    approved_refs is a list of (row_num, url, resolved_ip).
    Downloads happen on-demand in the async phase.
    """
    semaphore = asyncio.Semaphore(GEMINI_CONCURRENT_LIMIT)
    rate_limiter = RateLimiter(GEMINI_RPM_LIMIT)
    results: list[dict] = []

    async def process_single(row_num: int, url: str, resolved_ip: str | None) -> dict:
        try:
            file_bytes = await asyncio.to_thread(
                _download_file, url, storage_client, resolved_ip,
            )
        except Exception as e:
            logger.error("Row %s: Download failed: %s", row_num, e)
            result = get_empty_result()
            result["_row"] = row_num
            result["_url"] = url
            result["_error"] = f"Download failed: {e}"
            return result

        ext = get_url_extension(url)
        ok, reason = validate_file_content(file_bytes, ext)
        if not ok:
            logger.warning("Row %s: Content rejected: %s", row_num, reason)
            result = get_empty_result()
            result["_row"] = row_num
            result["_url"] = url
            result["_error"] = reason
            return result

        row_info = {"row": row_num, "url": url}
        result = await extract_with_gemini_async(
            file_bytes, ext.lstrip("."), gemini_client, semaphore, rate_limiter, row_info,
        )
        result["_row"] = row_num
        result["_url"] = url
        return result

    tasks = [
        asyncio.create_task(process_single(row_num, url, resolved_ip))
        for row_num, url, resolved_ip in approved_refs
    ]

    completed = 0
    total = len(tasks)
    for coro in asyncio.as_completed(tasks):
        result = await coro
        results.append(result)
        completed += 1
        if completed % 10 == 0 or completed == total:
            logger.info("Progress: %d/%d invoices processed", completed, total)

        # Honour SIGTERM — cancel remaining tasks and return partial results
        if _shutdown_event.is_set():
            logger.warning("Shutdown requested — stopping at %d/%d", completed, total)
            for t in tasks:
                if not t.done():
                    t.cancel()
            break

    results.sort(key=lambda x: x.get("_row", 0))
    return results


# ══════════════════════════════════════════════════════════════
#  FIELD VALIDATION & CLEANING
# ══════════════════════════════════════════════════════════════

def _apply_numeric_banking_prefix(raw: str, expected_digits: int | None = None) -> str:
    """Strip hyphens/spaces, validate digit length, prepend apostrophe.

    The apostrophe prevents Excel/CSV from dropping leading zeros.
    Returns formatted value (e.g. "'200000") or empty string if invalid.
    """
    if not raw:
        return ""
    raw = raw.lstrip("'")
    digits = re.sub(r"[\s\-]", "", str(raw))
    if not digits.isdigit():
        return ""
    if expected_digits is not None and len(digits) != expected_digits:
        return ""
    return "'" + digits


def validate_and_clean_fields(data: dict) -> dict:
    """Validate and clean all extracted fields."""
    clean: dict = {}
    clean["_row"] = data.get("_row")

    # ── Banking ──────────────────────────────────────────

    name = data.get("Account_Name", "")
    clean["Account_Name"] = (
        re.sub(r"[^A-Za-z0-9\s&'.\-]", "", str(name)).strip() if name else ""
    )

    clean["Bank_Name"] = str(data.get("Bank_Name", "")).strip()[:100]

    # Account Number — 6-18 digits for international support
    acct = str(data.get("Account_Number", "")).strip()
    if acct:
        acct_stripped = acct.lstrip("'")
        acct_digits = re.sub(r"[\s\-]", "", acct_stripped)
        acct_match = re.search(r"\d{6,18}", acct_digits)
        clean["Account_Number"] = "'" + acct_match.group(0) if acct_match else ""
    else:
        clean["Account_Number"] = ""

    # Sort Code — UK 6-digit
    sort_code = str(data.get("Sort_Code", "")).strip()
    if sort_code:
        sc_stripped = sort_code.lstrip("'")
        sc_digits = re.sub(r"[\s\-]", "", sc_stripped)
        sc_match = re.search(r"\d{6}", sc_digits)
        clean["Sort_Code"] = "'" + sc_match.group(0) if sc_match else ""
    else:
        clean["Sort_Code"] = ""

    clean["Routing_Number"] = _apply_numeric_banking_prefix(
        str(data.get("Routing_Number", "")).strip(), expected_digits=9)
    clean["Transit_Number"] = _apply_numeric_banking_prefix(
        str(data.get("Transit_Number", "")).strip(), expected_digits=5)
    clean["Branch_Code"] = _apply_numeric_banking_prefix(
        str(data.get("Branch_Code", "")).strip(), expected_digits=6)

    # SWIFT/BIC
    bic = data.get("SWIFT_BIC", "")
    bic_val = ""
    if bic:
        bic_clean = re.sub(r"\s+", "", str(bic)).upper()
        bic_match = re.search(r"\b[A-Z]{6}[A-Z0-9]{2}([A-Z0-9]{3})?\b", bic_clean)
        if bic_match:
            bic_val = bic_match.group(0)
    clean["SWIFT_BIC"] = bic_val

    # IBAN
    iban = data.get("IBAN", "")
    iban_val = ""
    if iban:
        iban_clean = re.sub(r"\s+", "", str(iban)).upper()
        iban_match = re.search(r"\b[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}\b", iban_clean)
        if iban_match:
            iban_val = iban_match.group(0)
    clean["IBAN"] = iban_val

    # ── Invoice details ──────────────────────────────────

    clean["Invoice_Number"] = str(data.get("Invoice_Number", "")).strip()[:50]

    for date_field in ("Invoice_Date", "Due_Date"):
        date_val = str(data.get(date_field, "")).strip()
        clean[date_field] = date_val[:20] if date_val else ""

    currency = str(data.get("Currency", "")).strip().upper()
    clean["Currency"] = (
        currency if (currency and len(currency) == 3 and currency.isalpha()) else ""
    )

    # Numeric amounts — preserve negative sign for credit notes
    for amount_field in ("Subtotal", "Tax_Amount", "Total_Amount"):
        amount = data.get(amount_field, "")
        if amount:
            amount_clean = re.sub(r"[^\d.\-]", "", str(amount))
            # Ensure at most one leading minus and valid float
            amount_clean = re.sub(r"(?!^)-", "", amount_clean)
            try:
                float(amount_clean)
                clean[amount_field] = amount_clean
            except ValueError:
                clean[amount_field] = ""
        else:
            clean[amount_field] = ""

    # Tax Rate — 0-100%
    tax_rate = data.get("Tax_Rate", "")
    if tax_rate:
        rate_clean = re.sub(r"[^\d.]", "", str(tax_rate))
        try:
            rate_val = float(rate_clean)
            clean["Tax_Rate"] = rate_clean if 0 <= rate_val <= 100 else ""
        except ValueError:
            clean["Tax_Rate"] = ""
    else:
        clean["Tax_Rate"] = ""

    # Document Type
    doc_type = str(data.get("Document_Type", "")).strip()
    valid_doc_types = {"Invoice", "Credit Note", "Pro Forma", "Statement", "Remittance Advice"}
    clean["Document_Type"] = doc_type if doc_type in valid_doc_types else "Invoice"

    clean["Credit_Note_Reference"] = str(data.get("Credit_Note_Reference", "")).strip()[:50]
    clean["Payment_Terms"] = str(data.get("Payment_Terms", "")).strip()[:100]

    # Job Number — agency format: 2-8 uppercase letters + 3-6 digits + optional /revision
    job_raw = str(data.get("Job_Number", "")).strip().upper()
    job_match = re.search(r"\b[A-Z]{2,8}\d{3,6}(?:/\d{1,3})?\b", job_raw)
    clean["Job_Number"] = job_match.group(0) if job_match else ""

    # Accounting Description
    acct_desc = str(data.get("Accounting_Description", "")).strip()[:120]
    if acct_desc and acct_desc.count(" - ") < 2:
        logger.warning(
            "Row %s: Accounting_Description missing expected ' - ' separators: %r",
            data.get("_row"), acct_desc,
        )
    clean["Accounting_Description"] = acct_desc

    # ── Vendor ───────────────────────────────────────────

    clean["Vendor_Name"] = str(data.get("Vendor_Name", "")).strip()[:200]

    vat = data.get("Vendor_TAX_Number", "")
    clean["Vendor_TAX_Number"] = (
        str(vat).strip().upper().replace(" ", "") if vat else ""
    )[:20]

    clean["Vendor_Address"] = str(data.get("Vendor_Address", "")).strip()[:300]

    return clean


# ══════════════════════════════════════════════════════════════
#  BIGQUERY / GCS HELPERS
# ══════════════════════════════════════════════════════════════

def format_for_bigquery(df: pd.DataFrame) -> pd.DataFrame:
    """Format DataFrame for BigQuery compatibility."""
    df = df.copy()
    df.columns = [
        re.sub(r"[./]", "_", str(col)).replace(" ", "_").upper()
        for col in df.columns
    ]
    # Normalise all whitespace — collapse CR/LF/TAB to a single space so
    # BigQuery's CSV loader (allowQuotedNewlines defaults to false) doesn't
    # break quoted fields across lines. Also strip NUL which corrupts CSV.
    df = (
        df.astype(str)
        .replace({"nan": "", "None": ""})
        .apply(
            lambda col: col.str.replace("\x00", "", regex=False)
            .str.replace(r"[\r\n\t]+", " ", regex=True)
            .str.replace(r" {2,}", " ", regex=True)
            .str.strip()
        )
    )

    # Apostrophe-prefix for invoice/reference numbers (prevent Excel numeric coercion).
    # Banking field apostrophes are applied during field cleaning — never double-prefixed.
    numeric_cols = ["INVOICE_NO_", "INVOICE_NUMBER"]
    for col in numeric_cols:
        if col in df.columns:
            mask = (df[col] != "") & (~df[col].str.startswith("'"))
            df.loc[mask, col] = "'" + df.loc[mask, col]
    return df


def export_to_gcs(
    df: pd.DataFrame, bucket_name: str, file_path: str, storage_client: storage.Client,
):
    """Export DataFrame to GCS as CSV."""
    if df.empty:
        logger.warning("No data to export")
        return
    # Use stdlib csv.writer for strict RFC 4180 escaping:
    # internal " becomes "" inside a quoted field. QUOTE_ALL + doublequote
    # guarantees every field is safely quoted regardless of contents.
    csv_buffer = io.StringIO()
    writer = csv.writer(
        csv_buffer,
        quoting=csv.QUOTE_ALL,
        quotechar='"',
        doublequote=True,
        lineterminator="\n",
    )
    writer.writerow(list(df.columns))
    for row in df.itertuples(index=False, name=None):
        writer.writerow(["" if v is None else str(v) for v in row])
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(file_path)
    blob.upload_from_string(csv_buffer.getvalue(), content_type="text/csv")
    logger.info("Exported results to gs://%s/%s", bucket_name, file_path)


def _find_original_column(df: pd.DataFrame, target: str) -> str | None:
    """Find column in DataFrame matching target name (case/format insensitive)."""
    tkey = re.sub(r"[ _./]", "", target).lower()
    for c in df.columns:
        if re.sub(r"[ _./]", "", str(c)).lower() == tkey:
            return c
    return None


# ══════════════════════════════════════════════════════════════
#  PIPELINE STAGES
# ══════════════════════════════════════════════════════════════

def load_input_data(storage_client: storage.Client) -> tuple[pd.DataFrame, bytes]:
    """Download and parse the input file (CSV or Excel).

    Returns (dataframe, raw_bytes).  Enforces size limits before parsing.
    """
    bucket_name, blob_name = parse_gcs_url(INPUT_GCS_PATH)
    raw_bytes = storage_client.bucket(bucket_name).blob(blob_name).download_as_bytes()

    if len(raw_bytes) > MAX_INPUT_SIZE:
        raise ValueError(
            f"Input file too large: {len(raw_bytes)} bytes (max {MAX_INPUT_SIZE})"
        )

    ext = os.path.splitext(blob_name)[1].lower()
    if ext == ".csv":
        df = pd.read_csv(io.BytesIO(raw_bytes))
    elif ext in (".xlsx", ".xls"):
        df = pd.read_excel(io.BytesIO(raw_bytes))
    else:
        raise ValueError(f"Unsupported input file format: {ext} (expected .csv or .xlsx)")

    if len(df) > MAX_INPUT_ROWS:
        raise ValueError(f"Input has too many rows: {len(df)} (max {MAX_INPUT_ROWS})")

    logger.info("Input loaded (%s): %d rows", ext, len(df))
    return df, raw_bytes


def validate_and_shard(df: pd.DataFrame) -> tuple[list, list]:
    """Validate file URLs and assign to this shard.

    Returns (approved_refs, rejected_files).
    approved_refs: list of (row_num, url, resolved_ip | None).
    """
    approved_refs = []
    rejected_files = []

    for i in range(len(df)):
        row_number = i + 1
        if ((row_number - 1) % SHARD_COUNT) != (SHARD_INDEX - 1):
            continue

        row = df.iloc[i]
        url = str(row.get("Link", "") or "")

        ok, reason, resolved_ip = should_process_file(url)
        if ok:
            approved_refs.append((row_number, url, resolved_ip))
            logger.info("Row %d: APPROVED", row_number)
        else:
            rejected_files.append({"row": row_number, "url": url, "reason": reason})
            logger.warning("Row %d: REJECTED (%s)", row_number, reason)

    return approved_refs, rejected_files


def save_rejected_files(rejected_files: list, storage_client: storage.Client):
    """Save rejected file list to GCS for review."""
    if not rejected_files:
        return
    try:
        rejected_df = pd.DataFrame(rejected_files)
        storage_client.bucket(EXPORT_BUCKET_NAME).blob(
            f"{EXPORT_PREFIX}/rejected_files_shard{SHARD_INDEX}.csv"
        ).upload_from_string(rejected_df.to_csv(index=False), content_type="text/csv")
        logger.info("%d rejected files saved", len(rejected_files))
    except Exception as e:
        logger.warning("Failed to save rejected files: %s", e)


def save_extraction_failures(all_results: list, storage_client: storage.Client):
    """Save invoices that failed Gemini extraction for manual review."""
    failures = [
        {"row": r["_row"], "url": r.get("_url", ""), "error": r.get("_error", "extraction_empty")}
        for r in all_results
        if not any(v for k, v in r.items() if not k.startswith("_"))
    ]
    if not failures:
        return
    try:
        failures_df = pd.DataFrame(failures)
        storage_client.bucket(EXPORT_BUCKET_NAME).blob(
            f"{EXPORT_PREFIX}/extraction_failures_shard{SHARD_INDEX}.csv"
        ).upload_from_string(failures_df.to_csv(index=False), content_type="text/csv")
        logger.info("%d extraction failures saved for review", len(failures))
    except Exception as e:
        logger.warning("Failed to save extraction failures: %s", e)


def merge_and_export(
    all_results: list,
    input_df: pd.DataFrame,
    storage_client: storage.Client,
):
    """Merge extracted results with original spreadsheet metadata and export."""
    cleaned_results = [validate_and_clean_fields(item) for item in all_results]
    cleaned_df = pd.DataFrame(cleaned_results)

    orig_df = input_df.copy()
    orig_df["_row"] = orig_df.index + 1

    # Passthrough columns: sourced from the input spreadsheet, not AI-extracted
    orig_col_map = {
        "OFFICE_CODE":               "Office Code",
        "OFFICE_NAME":               "Office Name",
        "MASTER_SUPPLIER_CODE":      "Master Supplier Code",
        "MASTER_SUPPLIER_NAME":      "Master Supplier Name",
        "SUPPLIER_CODE":             "Supplier Code",
        "SUPPLIER_NAME":             "Supplier Name",
        "INVOICE_NO_":               "Invoice No.",
        "EXPECTED_JOB_NUMBER":       "Job/Schedule/Estimate No.",
        "LINK":                      "Link",
    }

    orig_selected = pd.DataFrame({"_row": orig_df["_row"]})
    for output_name, search_name in orig_col_map.items():
        match = _find_original_column(orig_df, search_name)
        orig_selected[output_name] = orig_df[match] if match else ""

    cleaned_df["_row"] = pd.to_numeric(cleaned_df["_row"], errors="coerce")
    orig_selected["_row"] = pd.to_numeric(orig_selected["_row"], errors="coerce")
    merged = pd.merge(orig_selected, cleaned_df, on="_row", how="left")

    desired_cols = [
        # System / passthrough
        "OFFICE_CODE", "OFFICE_NAME",
        "MASTER_SUPPLIER_CODE", "MASTER_SUPPLIER_NAME",
        "SUPPLIER_CODE", "SUPPLIER_NAME",
        "INVOICE_NO_", "EXPECTED_JOB_NUMBER",
        "LINK",
        # OCR: banking
        "ACCOUNT_NAME", "BANK_NAME", "ACCOUNT_NUMBER",
        "SORT_CODE", "ROUTING_NUMBER", "TRANSIT_NUMBER", "BRANCH_CODE",
        "SWIFT_BIC", "IBAN",
        # OCR: invoice
        "INVOICE_NUMBER", "INVOICE_DATE", "DUE_DATE", "CURRENCY", "SUBTOTAL",
        "TAX_AMOUNT", "TAX_RATE", "TOTAL_AMOUNT",
        "DOCUMENT_TYPE", "CREDIT_NOTE_REFERENCE", "PAYMENT_TERMS",
        "JOB_NUMBER", "ACCOUNTING_DESCRIPTION",
        # OCR: vendor
        "VENDOR_NAME", "VENDOR_TAX_NUMBER", "VENDOR_ADDRESS",
    ]

    merged.columns = [
        re.sub(r"[./]", "_", str(col)).replace(" ", "_").upper()
        for col in merged.columns
    ]
    for col in desired_cols:
        if col not in merged.columns:
            merged[col] = ""

    extra_cols = [c for c in merged.columns if c not in desired_cols and c != "_ROW"]
    final_df = merged.reindex(columns=desired_cols + extra_cols)
    final_df = format_for_bigquery(final_df)

    shard_suffix = f"_shard{SHARD_INDEX}"
    if FILE_OVERWRITE_NAME:
        base_name = FILE_OVERWRITE_NAME.removesuffix(".csv")
        filename = f"{base_name}{shard_suffix}.csv"
    else:
        filename = f"export{shard_suffix}.csv"

    export_path = f"{EXPORT_PREFIX}/{filename}"
    export_to_gcs(final_df, EXPORT_BUCKET_NAME, export_path, storage_client)
    logger.info("Export complete: gs://%s/%s", EXPORT_BUCKET_NAME, export_path)


# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════

def main():
    validate_config()
    logger.info("Cloud Run OCR worker starting (shard %s/%s)", SHARD_INDEX, SHARD_COUNT)
    logger.info("Using Gemini model: %s", GEMINI_MODEL)

    # ── Initialise clients ───────────────────────────────
    try:
        gemini_client = genai.Client(vertexai=True, project=PROJECT_ID, location=LOCATION)
        logger.info("Gemini client initialised (Vertex AI)")
        storage_client = storage.Client()
        logger.info("Storage client initialised")
    except Exception as e:
        logger.error("Failed to initialise clients: %s\n%s", e, traceback.format_exc())
        sys.exit(1)

    # ── Load input data ──────────────────────────────────
    try:
        df, _ = load_input_data(storage_client)
    except Exception as e:
        logger.error("Failed to load input file: %s", e)
        sys.exit(3)

    # ── Validate & shard ─────────────────────────────────
    approved_refs, rejected_files = validate_and_shard(df)
    save_rejected_files(rejected_files, storage_client)

    logger.info("Shard %d: %d approved files to process", SHARD_INDEX, len(approved_refs))
    if not approved_refs:
        logger.info("No approved files for this shard — exiting gracefully")
        return

    # ── Gemini extraction ────────────────────────────────
    logger.info("Starting async Gemini extraction for %d invoices", len(approved_refs))
    start_time = time.time()

    try:
        all_results = asyncio.run(
            process_all_invoices_async(approved_refs, gemini_client, storage_client)
        )
        logger.info("Gemini extraction completed in %.1fs", time.time() - start_time)
    except Exception as e:
        logger.error("Gemini extraction failed: %s\n%s", e, traceback.format_exc())
        sys.exit(1)

    success_count = sum(
        1 for r in all_results if any(v for k, v in r.items() if not k.startswith("_"))
    )
    logger.info(
        "Results: %d total, %d with data, %d empty",
        len(all_results), success_count, len(all_results) - success_count,
    )

    # ── Save extraction failures for review ──────────────
    save_extraction_failures(all_results, storage_client)

    # ── Merge & export ───────────────────────────────────
    try:
        merge_and_export(all_results, df, storage_client)
    except Exception as e:
        logger.error("Failed during final merge/export: %s\n%s", e, traceback.format_exc())
        sys.exit(1)

    logger.info("Worker finished successfully for shard %s/%s", SHARD_INDEX, SHARD_COUNT)


if __name__ == "__main__":
    main()
