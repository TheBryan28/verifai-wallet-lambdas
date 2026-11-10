"""index-corpus: builds one versioned layer of the legal corpus (one jurisdiction, one version).

Run it manually from the Lambda console with a test event:
    {"code": "CO", "version": "2026-11", "activate": true}
    {"code": "ES", "version": "2026-11", "parent": "EU", "activate": true}

Reads   corpus-source/{code}/{version}/{doc_id}.txt   full text of a law or a ruling
        corpus-source/{code}/{version}/{doc_id}.json  {"norm", "type", "url", "derogated_articles"}
Writes  corpus/{code}/{version}/meta.json      model, dimensions, tokenizer, documents
        corpus/{code}/{version}/chunks.jsonl   one fragment per line (row i of index.npy)
        corpus/{code}/{version}/index.npy      float32 embeddings, L2-normalized
        corpus/{code}/{version}/lexical.json   BM25 postings for keyword search
        corpus/manifest.json                   only when "activate" is true

Embeddings are cached per document in corpus-cache/, so running it again only
embeds documents that changed. If the time budget runs out, run it again: it
continues from the cache.
"""
import hashlib
import io
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import boto3
import numpy as np
from botocore.config import Config
from botocore.exceptions import ClientError

import lexical

BUCKET = os.environ["BUCKET"]
EMBEDDING_MODEL_ID = os.environ.get("EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0")
EMBEDDING_DIMENSIONS = int(os.environ.get("EMBEDDING_DIMENSIONS", "1024"))
BEDROCK_REGION = os.environ.get("BEDROCK_REGION", os.environ["AWS_REGION"])
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "8"))
MAX_CHUNK_WORDS = 800          # longer articles are split by paragraphs
CASE_LAW_BLOCK_WORDS = 400     # rulings have no articles: fixed blocks with overlap
CASE_LAW_OVERLAP_WORDS = 50
TIME_RESERVE_MS = 90_000       # stop before Lambda's timeout and ask to run again
SOURCE_PREFIX = "corpus-source"
CORPUS_PREFIX = "corpus"
CACHE_PREFIX = "corpus-cache"
EU_MEMBERS = ["AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GR", "HR", "HU", "IE",
              "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK"]

s3 = boto3.client("s3")
bedrock = boto3.client(
    "bedrock-runtime",
    region_name=BEDROCK_REGION,
    config=Config(retries={"max_attempts": 10, "mode": "adaptive"}),
)

# ARTÍCULO 24.  Artículo 9.  ARTÍCULO 1o. (Colombian ordinal "o")  ARTÍCULO 5A.  ARTÍCULO 25 BIS.  Artículo único.
ARTICLE_HEADING = re.compile(
    r"^\s*ART[ÍI]CULO\s+(?P<num>\d+(?:-?(?-i:[A-Z]))?(?:\s+bis)?|[ÚU]NICO)\s*[oº°]?\s*[.:\-–—]",
    re.IGNORECASE | re.MULTILINE,
)
DEROGATION_HINT = re.compile(r"\bderogad[oa]s?\b", re.IGNORECASE)


class InvalidRequest(Exception):
    pass


class SourceNotFound(Exception):
    pass


class InvalidSource(Exception):
    pass


class TimeBudgetExceeded(Exception):
    pass


class ManifestChanged(Exception):
    pass


def lambda_handler(event, context):
    code = str(event.get("code", "")).upper()
    version = str(event.get("version", ""))
    if not re.fullmatch(r"[A-Z]{2,3}", code):
        raise InvalidRequest("code must be a 2 or 3 letter jurisdiction code, such as CO, ES or EU")
    if not re.fullmatch(r"[0-9A-Za-z._-]{1,40}", version):
        raise InvalidRequest("version may only contain letters, digits, '.', '_' and '-'")

    documents = list_documents(code, version)
    chunks, vectors = [], []
    excluded, review, embedded_now, from_cache = [], [], 0, 0

    for document in documents:
        if context.get_remaining_time_in_millis() < TIME_RESERVE_MS:
            raise TimeBudgetExceeded(
                f"Indexed {len(vectors)} of {len(documents)} documents; run again to continue from the cache"
            )
        doc_chunks, doc_excluded = chunk_document(code, document)
        excluded += doc_excluded
        review += [c["id"] for c in doc_chunks if DEROGATION_HINT.search(c["text"][:300])]
        doc_vectors, cached = embed_document(doc_chunks)
        from_cache += len(doc_chunks) if cached else 0
        embedded_now += 0 if cached else len(doc_chunks)
        chunks += doc_chunks
        vectors.append(doc_vectors)

    if not chunks:
        raise InvalidSource("The source documents produced no fragments")
    index = np.vstack(vectors).astype(np.float32)

    prefix = f"{CORPUS_PREFIX}/{code}/{version}/"
    lexical_index = lexical.build_lexical_index([search_text(c) for c in chunks])
    meta = {
        "code": code,
        "version": version,
        "embedding_model": EMBEDDING_MODEL_ID,
        "dimensions": EMBEDDING_DIMENSIONS,
        "count": len(chunks),
        "tokenizer": lexical.TOKENIZER_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "documents": [
            {"doc_id": d["doc_id"], "norm": d["norm"], "type": d["type"],
             "chunks": sum(1 for c in chunks if c["doc_id"] == d["doc_id"])}
            for d in documents
        ],
    }
    buffer = io.BytesIO()
    np.save(buffer, index)
    put_bytes(prefix + "index.npy", buffer.getvalue(), "application/octet-stream")
    put_bytes(prefix + "chunks.jsonl",
              "\n".join(json.dumps(c, ensure_ascii=False) for c in chunks).encode("utf-8"),
              "application/x-ndjson")
    put_bytes(prefix + "lexical.json", json.dumps(lexical_index).encode("utf-8"), "application/json")
    # meta.json goes last: its presence means the other three files are complete
    put_bytes(prefix + "meta.json", json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8"),
              "application/json")

    activated = False
    if event.get("activate"):
        activate_version(code, version, event.get("parent"))
        activated = True

    return {
        "code": code,
        "version": version,
        "documents": len(documents),
        "chunks": len(chunks),
        "embedded_now": embedded_now,
        "from_cache": from_cache,
        "excluded_derogated": excluded,
        "review_possible_derogation": review,
        "activated": activated,
    }


# ---------- sources ----------

def list_documents(code, version):
    prefix = f"{SOURCE_PREFIX}/{code}/{version}/"
    keys, token = [], None
    while True:
        params = {"Bucket": BUCKET, "Prefix": prefix}
        if token:
            params["ContinuationToken"] = token
        page = s3.list_objects_v2(**params)
        keys += [o["Key"] for o in page.get("Contents", [])]
        if not page.get("IsTruncated"):
            break
        token = page["NextContinuationToken"]

    text_keys = sorted(k for k in keys if k.endswith(".txt"))
    if not text_keys:
        raise SourceNotFound(f"No .txt files under {prefix}")

    documents = []
    for text_key in text_keys:
        doc_id = re.sub(r"[^0-9A-Za-z_-]+", "-", text_key[len(prefix):-4]).strip("-")
        metadata_key = text_key[:-4] + ".json"
        if metadata_key not in keys:
            raise InvalidSource(f"Missing metadata file {metadata_key}")
        metadata = json.loads(get_bytes(metadata_key))
        if not metadata.get("norm"):
            raise InvalidSource(f"{metadata_key} needs a 'norm' field with the name of the law or ruling")
        doc_type = metadata.get("type", "law")
        if doc_type not in ("law", "case_law"):
            raise InvalidSource(f"{metadata_key}: type must be 'law' or 'case_law'")
        documents.append({
            "doc_id": doc_id,
            "norm": metadata["norm"],
            "type": doc_type,
            "url": metadata.get("url", ""),
            "derogated_articles": {normalize_article(a) for a in metadata.get("derogated_articles", [])},
            "text": get_bytes(text_key).decode("utf-8"),
        })
    return documents


# ---------- chunking ----------

def normalize_article(number):
    return re.sub(r"\s+", "", str(number)).lower().replace("ú", "u")


def chunk_document(code, document):
    text = re.sub(r"[ \t]+", " ", document["text"].replace("\r\n", "\n").replace(" ", " ")).strip()
    matches = list(ARTICLE_HEADING.finditer(text)) if document["type"] == "law" else []

    if not matches:
        parts = [("", f"Block {i}", block)
                 for i, block in enumerate(word_blocks(text, CASE_LAW_BLOCK_WORDS, CASE_LAW_OVERLAP_WORDS), 1)]
    else:
        parts = []
        for position, match in enumerate(matches):
            end = matches[position + 1].start() if position + 1 < len(matches) else len(text)
            body = text[match.start():end].strip()
            heading = body.split("\n", 1)[0][:160]
            parts.append((normalize_article(match.group("num")), heading, body))

    chunks, excluded, seen_ids = [], [], {}
    for article, heading, body in parts:
        base_id = f"{code}-{document['doc_id']}-" + (f"art{article}" if article else heading.lower().replace(" ", ""))
        # Some laws repeat numbers (e.g. transitional articles): keep ids unique
        seen_ids[base_id] = seen_ids.get(base_id, 0) + 1
        if seen_ids[base_id] > 1:
            base_id = f"{base_id}-{seen_ids[base_id]}"
        if article and article in document["derogated_articles"]:
            excluded.append(base_id)
            continue
        pieces = split_by_paragraphs(body, MAX_CHUNK_WORDS)
        for number, piece in enumerate(pieces, 1):
            chunks.append({
                "id": base_id if len(pieces) == 1 else f"{base_id}-p{number}",
                "doc_id": document["doc_id"],
                "norm": document["norm"],
                "type": document["type"],
                "article": article,
                "heading": heading,
                "text": piece,
                "url": document["url"],
            })
    return chunks, excluded


def split_by_paragraphs(text, max_words):
    if len(text.split()) <= max_words:
        return [text]
    pieces, current = [], []
    for paragraph in re.split(r"\n\s*\n|\n", text):
        words = paragraph.split()
        if current and len(current) + len(words) > max_words:
            pieces.append(" ".join(current))
            current = []
        current += words
        while len(current) > max_words:
            pieces.append(" ".join(current[:max_words]))
            current = current[max_words:]
    if current:
        pieces.append(" ".join(current))
    return pieces


def word_blocks(text, size, overlap):
    words = text.split()
    if len(words) <= size:
        return [" ".join(words)] if words else []
    step = size - overlap
    return [" ".join(words[start:start + size]) for start in range(0, len(words) - overlap, step)]


def search_text(chunk):
    """Text that is embedded and indexed: the fragment plus where it comes from."""
    return f"{chunk['norm']} · {chunk['heading']}\n{chunk['text']}"


# ---------- embeddings ----------

def embed(text):
    response = bedrock.invoke_model(
        modelId=EMBEDDING_MODEL_ID,
        contentType="application/json",
        accept="application/json",
        body=json.dumps({"inputText": text[:20000], "dimensions": EMBEDDING_DIMENSIONS, "normalize": True}),
    )
    return json.loads(response["body"].read())["embedding"]


def embed_document(chunks):
    """Returns (matrix, came_from_cache). The cache key covers the model, dimensions and every input."""
    inputs = [search_text(c) for c in chunks]
    digest = hashlib.sha256(json.dumps(
        {"model": EMBEDDING_MODEL_ID, "dimensions": EMBEDDING_DIMENSIONS, "inputs": inputs},
        ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    cache_key = f"{CACHE_PREFIX}/{digest}.npy"

    try:
        return np.load(io.BytesIO(get_bytes(cache_key))), True
    except SourceNotFound:
        pass

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        matrix = np.asarray(list(pool.map(embed, inputs)), dtype=np.float32)
    buffer = io.BytesIO()
    np.save(buffer, matrix)
    put_bytes(cache_key, buffer.getvalue(), "application/octet-stream")
    return matrix, False


# ---------- manifest ----------

def activate_version(code, version, parent):
    """Points the manifest at this version, with a conditional write so concurrent edits are not lost."""
    key = f"{CORPUS_PREFIX}/manifest.json"
    try:
        response = s3.get_object(Bucket=BUCKET, Key=key)
        manifest, condition = json.loads(response["Body"].read()), {"IfMatch": response["ETag"]}
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("NoSuchKey", "404"):
            raise
        manifest, condition = {"jurisdictions": {}, "eu_members": EU_MEMBERS}, {"IfNoneMatch": "*"}

    jurisdictions = manifest.setdefault("jurisdictions", {})
    entry = jurisdictions.setdefault(code, {"parent": None})
    if parent is not None:
        parent = str(parent).upper() or None
        if parent and parent not in jurisdictions:
            raise InvalidRequest(f"Index and activate the parent layer {parent} first")
        entry["parent"] = parent
    entry["active_version"] = version
    entry["activated_at"] = datetime.now(timezone.utc).isoformat()

    try:
        s3.put_object(Bucket=BUCKET, Key=key, ContentType="application/json",
                      Body=json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"), **condition)
    except ClientError as e:
        if e.response["Error"]["Code"] in ("PreconditionFailed", "ConditionalRequestConflict"):
            raise ManifestChanged("The manifest changed while activating; run again") from e
        raise


# ---------- S3 helpers ----------

def get_bytes(key):
    try:
        return s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
            raise SourceNotFound(key) from e
        raise


def put_bytes(key, body, content_type):
    s3.put_object(Bucket=BUCKET, Key=key, Body=body, ContentType=content_type)
