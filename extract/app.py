"""extract: validates the uploaded document, extracts its text page by page, pseudonymizes
personal data and splits the contract into clauses, so the analyze step never sees the
original file.

Input (from Step Functions):  {"analysis_id", "bucket", "key", "size"}
Output:                       {"processed_key", "clause_count"}
Writes:                       processed/{analysis_id}/text.json

Errors are raised as named exceptions; Step Functions stores the class name in
the analysis error_message, so the app can show a specific message.
"""
import io
import json
import os
import re
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError
from pypdf import PdfReader
from pypdf.errors import PdfReadError

ANALYSES_TABLE = os.environ["ANALYSES_TABLE"]
TEXTRACT_REGION = os.environ.get("TEXTRACT_REGION", os.environ["AWS_REGION"])
MAX_SIZE_BYTES = int(os.environ.get("MAX_SIZE_BYTES", str(10 * 1024 * 1024)))
REQUIRE_CHECKSUM = os.environ.get("REQUIRE_CHECKSUM", "false").lower() == "true"
MIN_PDF_TEXT_CHARS = 200      # less than this from a PDF means it is a scan
MIN_HEADINGS = 3              # fewer headings than this: the document has no clause structure
MIN_CLAUSE_WORDS = 50         # shorter numbered items (5., 5.1) are merged into the previous clause
MIN_FORMAL_CLAUSE_WORDS = 15  # formal clauses (PRIMERA., CLÁUSULA 5) are merged only when this short
MAX_CLAUSE_WORDS = 800        # longer clauses are split by paragraphs
FALLBACK_BLOCK_WORDS = 400    # block size when no headings are found
FALLBACK_OVERLAP_WORDS = 50

s3 = boto3.client("s3")
dynamodb = boto3.client("dynamodb")
textract = boto3.client("textract", region_name=TEXTRACT_REGION)

MAGIC_BYTES = {
    "application/pdf": b"%PDF-",
    "image/png": b"\x89PNG\r\n\x1a\n",
    "image/jpeg": b"\xff\xd8\xff",
}


class AnalysisNotFound(Exception):
    pass


class FileTooLarge(Exception):
    pass


class FileSizeMismatch(Exception):
    pass


class ChecksumMismatch(Exception):
    pass


class UnsupportedFileType(Exception):
    pass


class ContentTypeMismatch(Exception):
    pass


class EncryptedPdf(Exception):
    pass


class InvalidFile(Exception):
    pass


class ScannedPdfNotSupported(Exception):
    pass


class NoTextFound(Exception):
    pass


def lambda_handler(event, context):
    analysis_id = event["analysis_id"]
    bucket = event["bucket"]
    key = event["key"]

    analysis = load_analysis(analysis_id)
    head = s3.head_object(Bucket=bucket, Key=key, ChecksumMode="ENABLED")
    validate_object(head, analysis)

    data = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    content_type = detect_content_type(data)
    declared = analysis.get("content_type")
    if declared and declared != content_type:
        raise ContentTypeMismatch(f"Declared {declared}, received {content_type}")

    if content_type == "application/pdf":
        pages, method = extract_pdf(data), "pdf_text"
    else:
        pages, method = [extract_image(data)], "textract"

    pages = remove_page_furniture([normalize_whitespace(page) for page in pages])
    pseudonymizer = Pseudonymizer()
    pages = [pseudonymizer.apply(page) for page in pages]
    if sum(len(page) for page in pages) < 20:
        raise NoTextFound("The document does not contain readable text")

    clauses = split_into_clauses(pages)

    processed_key = f"processed/{analysis_id}/text.json"
    s3.put_object(
        Bucket=bucket,
        Key=processed_key,
        ContentType="application/json",
        Body=json.dumps({
            "analysis_id": analysis_id,
            "country": analysis["country"],
            "content_type": content_type,
            "extraction_method": method,
            "pages": len(pages),
            "pii_replacements": pseudonymizer.counts(),
            "clauses": clauses,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }, ensure_ascii=False).encode("utf-8"),
    )
    return {"processed_key": processed_key, "clause_count": len(clauses)}


# ---------- validation ----------

def load_analysis(analysis_id):
    item = dynamodb.get_item(
        TableName=ANALYSES_TABLE, Key={"id": {"S": analysis_id}}, ConsistentRead=True
    ).get("Item")
    if not item:
        raise AnalysisNotFound(analysis_id)
    return {
        "country": item["country"]["S"],
        "content_type": item.get("content_type", {}).get("S"),
        "sha256": item.get("sha256", {}).get("S"),
        "size_bytes": int(item["size_bytes"]["N"]) if "size_bytes" in item else None,
    }


def validate_object(head, analysis):
    size = head["ContentLength"]
    if size > MAX_SIZE_BYTES:
        raise FileTooLarge(f"{size} bytes, limit is {MAX_SIZE_BYTES}")
    if analysis["size_bytes"] is not None and size != analysis["size_bytes"]:
        raise FileSizeMismatch(f"Declared {analysis['size_bytes']} bytes, received {size}")

    stored_checksum = head.get("ChecksumSHA256")
    if stored_checksum is None:
        if REQUIRE_CHECKSUM:
            raise ChecksumMismatch("The object was uploaded without a SHA-256 checksum")
    elif analysis["sha256"] and stored_checksum != analysis["sha256"]:
        raise ChecksumMismatch("The object checksum does not match the declared SHA-256")


def detect_content_type(data):
    # Trust the file's own bytes, not the Content-Type header or the file name
    for content_type, magic in MAGIC_BYTES.items():
        if data.startswith(magic):
            return content_type
    raise UnsupportedFileType("Only PDF, PNG and JPEG files are supported")


# ---------- text extraction ----------

def extract_pdf(data):
    """One text per page, so every clause can keep the page where it starts."""
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise EncryptedPdf("The PDF is password protected")
        pages = [page.extract_text() or "" for page in reader.pages]
    except PdfReadError as e:
        raise InvalidFile(f"The PDF could not be read: {e}") from e

    if sum(len(page.strip()) for page in pages) < MIN_PDF_TEXT_CHARS:
        # Scanned PDFs need asynchronous Textract (StartDocumentTextDetection)
        raise ScannedPdfNotSupported("The PDF has no text layer; upload a photo of each page instead")
    return pages


def extract_image(data):
    try:
        response = textract.detect_document_text(Document={"Bytes": data})
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("DocumentTooLargeException", "ProvisionedThroughputExceededException"):
            raise FileTooLarge("The image is too large for text detection") from e
        if code in ("UnsupportedDocumentException", "BadDocumentException", "InvalidParameterException"):
            raise InvalidFile(f"The image could not be processed: {code}") from e
        raise
    return "\n".join(b["Text"] for b in response["Blocks"] if b["BlockType"] == "LINE")


def normalize_whitespace(text):
    text = text.replace("\r\n", "\n").replace(" ", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


PAGE_NUMBER_LINE = re.compile(r"^\s*(?:p[áa]g(?:ina)?\.?\s*)?\d{1,4}\s*(?:(?:de|/)\s*\d{1,4})?\s*$", re.IGNORECASE)


def remove_page_furniture(pages):
    """Drops headers and footers: lines repeated on most pages (digits ignored) and lone page numbers."""
    def key(line):
        return re.sub(r"\d+", "#", line.strip().lower())

    repeated = set()
    if len(pages) >= 2:
        counts = {}
        for page in pages:
            for line_key in {key(line) for line in page.split("\n") if line.strip()}:
                counts[line_key] = counts.get(line_key, 0) + 1
        threshold = max(2, -(-len(pages) * 6 // 10))  # on at least 60% of the pages
        repeated = {k for k, n in counts.items() if n >= threshold and len(k) <= 120}

    return [
        "\n".join(line for line in page.split("\n")
                  if key(line) not in repeated and not PAGE_NUMBER_LINE.match(line)).strip()
        for page in pages
    ]


# ---------- pseudonymization ----------

# Order matters: the most specific patterns run first.
PII_PATTERNS = [
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){3,7}(?:[ ]?[A-Z0-9]{1,4})?\b")),
    ("CARD", re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")),
    ("ES_ID", re.compile(r"\b[XYZ]?\d{7,8}-?[A-HJ-NP-TV-Z]\b")),
    ("CO_ID", re.compile(
        r"(?i)(?:\bC\.?\s?C\.?|c[ée]dula(?:\s+de\s+ciudadan[íi]a)?)(?:\s+(?:No\.?|n[úu]mero))?\s*:?\s*"
        r"(\d{1,3}(?:\.\d{3}){1,3}|\d{6,10})"
    )),
    # Tax ids ("NIT 900.000.000-0") run before PHONE, which would otherwise take their digits
    ("TAX_ID", re.compile(r"(?i)\bNIT\.?\s*(?:No\.?\s*)?:?\s*(\d{3}\.?\d{3}\.?\d{3}(?:\s*-\s*\d)?)")),
    # Separators may include a line break: PDF text often wraps a number across two lines
    ("PHONE", re.compile(
        r"(?<!\d)(?:\+?(?:34|57)[\s.-]?)?(?:[6-9]\d{2}[\s.-]?\d{3}[\s.-]?\d{3}|3\d{2}[\s.-]?\d{3}[\s.-]?\d{4})(?!\d)"
    )),
]


class Pseudonymizer:
    """Replaces personal data with stable tokens such as [EMAIL_1], consistent across pages.

    The token map only lives in memory during this invocation: the report refers to
    tokens, never to the original values. Person names are not detected by these
    patterns (see Amazon Comprehend DetectPiiEntities for that).
    """

    def __init__(self):
        self.seen = {label: {} for label, _ in PII_PATTERNS}

    def apply(self, text):
        for label, pattern in PII_PATTERNS:
            seen = self.seen[label]

            def replace(match, label=label, seen=seen):
                group = 1 if match.re.groups else 0
                value = match.group(group)
                token = seen.setdefault(value, f"[{label}_{len(seen) + 1}]")
                return token if group == 0 else match.group(0).replace(value, token)

            text = pattern.sub(replace, text)
        return text

    def counts(self):
        return {label: len(values) for label, values in self.seen.items() if values}


# ---------- clause segmentation ----------

ORDINALS = (
    r"PRIMER[AO]|SEGUND[AO]|TERCER[AO]|CUART[AO]|QUINT[AO]|SEXT[AO]|S[ÉE]PTIM[AO]|OCTAV[AO]|NOVEN[AO]"
    r"|D[ÉE]CIM[AO](?:\s+\w+)?|UND[ÉE]CIM[AO]|DUOD[ÉE]CIM[AO]|VIG[ÉE]SIM[AO](?:\s+\w+)?"
)
# Formal headings: PRIMERA.  CLÁUSULA 5  CLÁUSULA QUINTA:  Artículo 5.
FORMAL_HEADING_RE = re.compile(
    r"^\s*(?:"
    r"(?:CL[ÁA]USULA|ART[ÍI]CULO)\s+(?:\d+(?:\.\d+)*|[A-ZÁÉÍÓÚÑ]+(?:\s+[A-ZÁÉÍÓÚÑ]+)?)\s*(?:[.:\-–—)]|$)"
    rf"|(?:{ORDINALS})\s*[.:\-–—]"
    r")",
    re.IGNORECASE,
)
# Numbered headings, which are also used for lists inside a clause: 5. Título  5.1 Título
NUMBERED_HEADING_RE = re.compile(r"^\s*\d{1,2}(?:\.\d{1,2})*\.?\s+[A-ZÁÉÍÓÚÑ]")


def is_heading(line):
    return bool(FORMAL_HEADING_RE.match(line) or NUMBERED_HEADING_RE.match(line))


def word_count(lines):
    return sum(len(line.split()) for _, line in lines)


def split_into_clauses(pages):
    """Splits by clause headings, merges short clauses, splits long ones and keeps the start page."""
    lines = [(page_number, line) for page_number, page in enumerate(pages, 1) for line in page.split("\n")]
    heading_count = sum(1 for _, line in lines if is_heading(line))
    if heading_count < MIN_HEADINGS:
        return number_clauses(fallback_blocks(lines))

    segments = []
    for page_number, line in lines:
        if is_heading(line) or not segments:
            heading = line.strip()[:120] if is_heading(line) else "Preamble"
            segments.append({"heading": heading, "formal": bool(FORMAL_HEADING_RE.match(line)), "lines": []})
        segments[-1]["lines"].append((page_number, line))

    merged, carry = [], []
    for segment in segments:
        segment["lines"] = carry + segment["lines"]
        carry = []
        minimum = MIN_FORMAL_CLAUSE_WORDS if segment["formal"] else MIN_CLAUSE_WORDS
        if word_count(segment["lines"]) < minimum:
            if merged:
                merged[-1]["lines"] += segment["lines"]
            else:
                carry = segment["lines"]  # a short first segment joins the next one
            continue
        merged.append(segment)
    if carry:
        merged.append({"heading": "Preamble", "lines": carry})

    clauses = []
    for segment in merged:
        parts = split_long_segment(segment["lines"])
        for number, part in enumerate(parts, 1):
            heading = segment["heading"] if len(parts) == 1 else f"{segment['heading']} (part {number})"
            clauses.append({"heading": heading, "lines": part})
    return number_clauses(clauses)


def split_long_segment(lines):
    if word_count(lines) <= MAX_CLAUSE_WORDS:
        return [lines]
    parts, current, current_words = [], [], 0
    for page_number, line in lines:
        words = len(line.split())
        if current and current_words + words > MAX_CLAUSE_WORDS:
            parts.append(current)
            current, current_words = [], 0
        if words > MAX_CLAUSE_WORDS:  # one huge line: cut it by words
            tokens = line.split()
            for start in range(0, len(tokens), MAX_CLAUSE_WORDS):
                parts.append([(page_number, " ".join(tokens[start:start + MAX_CLAUSE_WORDS]))])
            continue
        current.append((page_number, line))
        current_words += words
    if current:
        parts.append(current)
    return parts


def fallback_blocks(lines):
    words = [(page_number, word) for page_number, line in lines for word in line.split()]
    if not words:
        return []
    step = FALLBACK_BLOCK_WORDS - FALLBACK_OVERLAP_WORDS
    starts = range(0, max(len(words) - FALLBACK_OVERLAP_WORDS, 1), step)
    return [
        {"heading": f"Block {number}",
         "lines": [(words[start][0], " ".join(w for _, w in words[start:start + FALLBACK_BLOCK_WORDS]))]}
        for number, start in enumerate(starts, 1)
    ]


def number_clauses(clauses):
    result = []
    for clause in clauses:
        text = "\n".join(line for _, line in clause["lines"]).strip()
        if not text:
            continue
        result.append({
            "index": len(result) + 1,
            "heading": clause["heading"],
            "page": clause["lines"][0][0],
            "word_count": len(text.split()),
            "text": text,
        })
    return result
