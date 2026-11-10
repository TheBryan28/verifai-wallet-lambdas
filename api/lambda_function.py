import base64
import hmac
import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

CONFIG_TABLE = os.environ["CONFIG_TABLE"]
ANALYSES_TABLE = os.environ["ANALYSES_TABLE"]
SESSION_QUOTAS_TABLE = os.environ["SESSION_QUOTAS_TABLE"]
BUCKET = os.environ["BUCKET"]
SIGNING_KEY_ID = os.environ["SIGNING_KEY_ID"]
TOKEN_ISSUER = os.environ["TOKEN_ISSUER"]
TOKEN_AUDIENCE = os.environ["TOKEN_AUDIENCE"]
APP_PUBLIC_KEY = os.environ["APP_PUBLIC_KEY"]

ALLOWED_CONTENT_TYPES = {"application/pdf", "image/jpeg", "image/png"}
MAX_SIZE_BYTES = 10 * 1024 * 1024
URL_EXPIRATION_SECONDS = 300      # 5 minutes
CONFIG_CACHE_SECONDS = 60         # how long the GLOBAL config is reused
QUOTA_TTL_DAYS = 2
ANALYSIS_TTL_DAYS = 30
SESSION_SECONDS = 24 * 60 * 60    # 1 day
SESSION_QUOTA_TTL_MARGIN_DAYS = 1
APP_KEY_HEADER = "x-verifai-app-key"

dynamodb = boto3.client("dynamodb")
kms = boto3.client("kms")
s3 = boto3.client(
    "s3",
    config=Config(
        signature_version="s3v4",
        s3={"addressing_style": "virtual"},
        request_checksum_calculation="when_required",
    ),
)
_config_cache = {"value": None, "loaded_at": 0.0}
_public_jwk_cache = {"value": None}


class AnalysisStatus(str, Enum):
    PENDING = "PENDING"         # created, waiting for the document upload
    PROCESSING = "PROCESSING"   # Step Functions running
    COMPLETED = "COMPLETED"     # report available at report_key
    FAILED = "FAILED"           # see error_message


def lambda_handler(event, context):
    method = event["requestContext"]["http"]["method"]
    path = event.get("rawPath", "/")
    try:
        if method == "POST" and path == "/sessions":
            return create_session(event)
        if method == "GET" and path == "/.well-known/openid-configuration":
            return openid_configuration()
        if method == "GET" and path == "/.well-known/jwks.json":
            return jwks()
        if path == "/analyses" or path.startswith("/analyses/"):
            # API Gateway validates the JWT. Without its claims (e.g. through a Function URL) nothing is served
            session = get_session(event)
            if session is None:
                return response(401, {"error": "Missing or invalid session token"})
            return route_analyses(method, path, event, session)
        return response(404, {"error": "Route not found"})
    except (ValueError, TypeError) as e:
        return response(400, {"error": str(e)})


def route_analyses(method, path, event, session):
    if method == "POST" and path == "/analyses":
        return create_analysis(parse_body(event), session)
    match = re.fullmatch(r"/analyses/([0-9a-f-]{36})(/upload-url)?", path)
    if match:
        analysis_id, upload_url_suffix = match.groups()
        if upload_url_suffix and method == "POST":
            return renew_upload_url(analysis_id)
        if not upload_url_suffix and method == "GET":
            return get_analysis(analysis_id)
        if not upload_url_suffix and method == "DELETE":
            return delete_analysis(analysis_id)
    return response(404, {"error": "Route not found"})


def get_session(event):
    authorizer = event["requestContext"].get("authorizer") or {}
    claims = (authorizer.get("jwt") or {}).get("claims") or {}
    if not claims.get("sub") or not claims.get("exp"):
        return None
    return {"id": claims["sub"], "expires_at": int(claims["exp"])}


def create_session(event):
    # The header only filters casual calls: its value can be extracted from the APK, so it is not authentication
    app_key = (event.get("headers") or {}).get(APP_KEY_HEADER, "")
    if not hmac.compare_digest(app_key.encode(), APP_PUBLIC_KEY.encode()):
        return response(403, {"error": "Invalid application key"})

    issued_at = int(time.time())
    expires_at = issued_at + SESSION_SECONDS
    token = sign_jwt({
        "iss": TOKEN_ISSUER,
        "aud": TOKEN_AUDIENCE,
        "sub": str(uuid.uuid4()),
        "iat": issued_at,
        "exp": expires_at,
    })
    return response(201, {
        "token": token,
        "token_type": "Bearer",
        "expires_at": datetime.fromtimestamp(expires_at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })


def sign_jwt(claims):
    header = {"alg": "RS256", "typ": "JWT", "kid": SIGNING_KEY_ID}
    signing_input = ".".join(
        base64url(json.dumps(part, separators=(",", ":")).encode()) for part in (header, claims)
    )
    signature = kms.sign(
        KeyId=SIGNING_KEY_ID,
        Message=signing_input.encode(),
        MessageType="RAW",
        SigningAlgorithm="RSASSA_PKCS1_V1_5_SHA_256",
    )["Signature"]
    return f"{signing_input}.{base64url(signature)}"


def openid_configuration():
    return response(200, {
        "issuer": TOKEN_ISSUER,
        "jwks_uri": f"{TOKEN_ISSUER}/.well-known/jwks.json",
    })


def jwks():
    if _public_jwk_cache["value"] is None:
        modulus, exponent = parse_rsa_public_key(kms.get_public_key(KeyId=SIGNING_KEY_ID)["PublicKey"])
        _public_jwk_cache["value"] = {
            "kty": "RSA",
            "use": "sig",
            "alg": "RS256",
            "kid": SIGNING_KEY_ID,
            "n": base64url(modulus),
            "e": base64url(exponent),
        }
    return response(200, {"keys": [_public_jwk_cache["value"]]})


def parse_rsa_public_key(der):
    # SubjectPublicKeyInfo: SEQUENCE { algorithm, BIT STRING { SEQUENCE { INTEGER n, INTEGER e } } }
    spki, _ = read_der_value(der, 0)
    _, next_offset = read_der_value(spki, 0)
    bit_string, _ = read_der_value(spki, next_offset)
    rsa_key, _ = read_der_value(bit_string, 1)  # the first byte counts the unused bits
    modulus, next_offset = read_der_value(rsa_key, 0)
    exponent, _ = read_der_value(rsa_key, next_offset)
    return modulus.lstrip(b"\x00"), exponent


def read_der_value(data, offset):
    length = data[offset + 1]
    offset += 2
    if length & 0x80:
        length_size = length & 0x7F
        length = int.from_bytes(data[offset:offset + length_size], "big")
        offset += length_size
    return data[offset:offset + length], offset + length


def base64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def parse_body(event):
    body = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body)
    return json.loads(body)


def get_global_config():
    if _config_cache["value"] is None or time.time() - _config_cache["loaded_at"] > CONFIG_CACHE_SECONDS:
        item = dynamodb.get_item(TableName=CONFIG_TABLE, Key={"id": {"S": "GLOBAL"}})["Item"]
        _config_cache["value"] = {
            "max_daily": int(item["max_daily"]["N"]),
            "max_per_session": int(item["max_per_session"]["N"]),
            "countries": {c["S"] for c in item["countries"]["L"]},
        }
        _config_cache["loaded_at"] = time.time()
    return _config_cache["value"]


def create_analysis(body, session):
    config = get_global_config()
    country = str(body.get("country", "")).upper()
    sha256 = str(body.get("sha256", ""))
    size_bytes = int(body.get("size_bytes", 0))
    content_type = str(body.get("content_type", "application/pdf"))
    if country not in config["countries"]:
        raise ValueError(f"Unsupported country: {country or '(empty)'}")
    if not re.fullmatch(r"[A-Za-z0-9+/]{43}=", sha256):
        raise ValueError("sha256 must be the base64-encoded SHA-256 digest (44 characters)")
    if not 0 < size_bytes <= MAX_SIZE_BYTES:
        raise ValueError(f"size_bytes must be between 1 and {MAX_SIZE_BYTES}")
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise ValueError(f"Unsupported content_type: {content_type}")

    now = datetime.now(timezone.utc)
    day = f"{now:%Y-%m-%d}"
    analysis_id = str(uuid.uuid4())
    document_key = f"uploads/{analysis_id}/document"

    session_quota_ttl = session["expires_at"] + SESSION_QUOTA_TTL_MARGIN_DAYS * 24 * 60 * 60

    # Daily quota, session quota and the PENDING analysis: all or nothing.
    # The session id is not stored with the analysis, so no record links a session to a contract.
    try:
        dynamodb.transact_write_items(TransactItems=[
            {"Update": {
                "TableName": CONFIG_TABLE,
                "Key": {"id": {"S": f"QUOTA#{day}"}},
                "UpdateExpression": "SET #ttl = if_not_exists(#ttl, :ttl) ADD #used :one",
                "ConditionExpression": "attribute_not_exists(#used) OR #used < :max",
                "ExpressionAttributeNames": {"#ttl": "ttl", "#used": "used"},
                "ExpressionAttributeValues": {
                    ":one": {"N": "1"},
                    ":max": {"N": str(config["max_daily"])},
                    ":ttl": {"N": str(to_epoch(now + timedelta(days=QUOTA_TTL_DAYS)))},
                },
            }},
            {"Update": {
                "TableName": SESSION_QUOTAS_TABLE,
                "Key": {"session_id": {"S": session["id"]}},
                "UpdateExpression": "SET #ttl = if_not_exists(#ttl, :ttl) ADD #used :one",
                "ConditionExpression": "attribute_not_exists(#used) OR #used < :max",
                "ExpressionAttributeNames": {"#ttl": "ttl", "#used": "used"},
                "ExpressionAttributeValues": {
                    ":one": {"N": "1"},
                    ":max": {"N": str(config["max_per_session"])},
                    ":ttl": {"N": str(session_quota_ttl)},
                },
            }},
            {"Put": {
                "TableName": ANALYSES_TABLE,
                "Item": {
                    "id": {"S": analysis_id},
                    "status": {"S": AnalysisStatus.PENDING.value},
                    "country": {"S": country},
                    "document_key": {"S": document_key},
                    "sha256": {"S": sha256},
                    "content_type": {"S": content_type},
                    "size_bytes": {"N": str(size_bytes)},
                    "created_at": {"S": now.isoformat()},
                    "day": {"S": day},
                    "ttl": {"N": str(to_epoch(now + timedelta(days=ANALYSIS_TTL_DAYS)))},
                },
                "ConditionExpression": "attribute_not_exists(#id)",
                "ExpressionAttributeNames": {"#id": "id"},
            }},
        ])
    except ClientError as e:
        reasons = [reason.get("Code") for reason in e.response.get("CancellationReasons", [])]
        if reasons[:1] == ["ConditionalCheckFailed"]:
            return response(429, {
                "code": "DAILY_LIMIT_REACHED",
                "error": f"Daily limit of {config['max_daily']} analyses reached (UTC). Try again tomorrow.",
            })
        if reasons[1:2] == ["ConditionalCheckFailed"]:
            return response(429, {
                "code": "SESSION_LIMIT_REACHED",
                "error": f"Session limit of {config['max_per_session']} analyses reached.",
            })
        if e.response["Error"]["Code"] == "TransactionCanceledException":
            return response(503, {"error": "Temporary conflict, please retry"})
        raise

    return response(201, {
        "id": analysis_id,
        "status": AnalysisStatus.PENDING.value,
        "upload": build_upload(document_key, content_type, sha256),
    })


def renew_upload_url(analysis_id):
    item = dynamodb.get_item(TableName=ANALYSES_TABLE, Key={"id": {"S": analysis_id}}).get("Item")
    if not item:
        return response(404, {"error": "Analysis not found"})
    if item["status"]["S"] != AnalysisStatus.PENDING.value:
        return response(409, {"error": "This analysis no longer accepts document uploads"})
    return response(200, {
        "id": analysis_id,
        "status": AnalysisStatus.PENDING.value,
        "upload": build_upload(item["document_key"]["S"], item["content_type"]["S"], item["sha256"]["S"]),
    })


def build_upload(document_key, content_type, sha256):
    # The URL only accepts a file with exactly this SHA-256 and Content-Type
    url = s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": BUCKET, "Key": document_key, "ContentType": content_type, "ChecksumSHA256": sha256},
        ExpiresIn=URL_EXPIRATION_SECONDS,
    )
    return {
        "method": "PUT",
        "url": url,
        "headers": {"Content-Type": content_type, "x-amz-checksum-sha256": sha256},
        "expires_in_seconds": URL_EXPIRATION_SECONDS,
    }


def get_analysis(analysis_id):
    item = dynamodb.get_item(TableName=ANALYSES_TABLE, Key={"id": {"S": analysis_id}}).get("Item")
    if not item:
        return response(404, {"error": "Analysis not found"})
    result = {
        "id": analysis_id,
        "status": item["status"]["S"],
        "country": item["country"]["S"],
        "created_at": item["created_at"]["S"],
        "from_cache": item.get("from_cache", {}).get("BOOL", False),
    }
    if "error_message" in item:
        result["error_message"] = item["error_message"]["S"]
    if result["status"] == AnalysisStatus.COMPLETED.value and "report_key" in item:
        result["report_url"] = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": BUCKET, "Key": item["report_key"]["S"]},
            ExpiresIn=URL_EXPIRATION_SECONDS,
        )
    return response(200, result)


def delete_analysis(analysis_id):
    key = {"id": {"S": analysis_id}}
    if "Item" not in dynamodb.get_item(TableName=ANALYSES_TABLE, Key=key):
        return response(404, {"error": "Analysis not found"})
    # Cached reports (reports/results/...) are shared between users and are not deleted here
    for prefix in ("uploads", "processed"):
        objects = s3.list_objects_v2(Bucket=BUCKET, Prefix=f"{prefix}/{analysis_id}/").get("Contents", [])
        if objects:
            s3.delete_objects(Bucket=BUCKET, Delete={"Objects": [{"Key": o["Key"]} for o in objects]})
    dynamodb.delete_item(TableName=ANALYSES_TABLE, Key=key)
    return response(204)


def to_epoch(moment):
    return int(moment.timestamp())


def response(status_code, body=None):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False) if body is not None else "",
    }
