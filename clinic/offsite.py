"""Off-site copy of encrypted backups to S3-compatible storage (docs/OFFLINE_SYNC_CONTRACT.md, section 11.2).

Works with AWS S3, Cloudflare R2, Backblaze B2 (S3 API), MinIO and others: any service that accepts a signed
`PUT <endpoint>/<bucket>/<key>`. Only the standard library is used (AWS Signature Version 4 is signed here).

Rules:
  * Only an **encrypted** backup is ever uploaded. A plain dump never leaves the clinic.
  * It is a copy, not a replacement for the second disk, and never part of the sync.
  * Old copies are not deleted from here: set a lifecycle rule on the bucket (for example keep 30 days).
"""
import datetime as dt
import hashlib
import hmac
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from django.conf import settings

from . import backup_crypto

ALGORITHM = "AWS4-HMAC-SHA256"
CHUNK = 1024 * 1024


class OffsiteError(Exception):
    """The off-site copy could not be made. The local backup is still good."""


def configured() -> bool:
    return all(getattr(settings, name, "") for name in ("BACKUP_S3_ENDPOINT", "BACKUP_S3_BUCKET", "BACKUP_S3_ACCESS_KEY", "BACKUP_S3_SECRET_KEY"))


# ---------- AWS Signature Version 4 ----------
def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret_key: str, date: str, region: str, service: str) -> bytes:
    k_date = _hmac(("AWS4" + secret_key).encode("utf-8"), date)
    return _hmac(_hmac(_hmac(k_date, region), service), "aws4_request")


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="-_.~")


def sign(method, url, *, region, service, access_key, secret_key, payload_hash, now, headers=None) -> dict:
    """Headers (including Authorization) for one request. `headers` are extra headers to sign."""
    parts = urllib.parse.urlsplit(url)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    date = now.strftime("%Y%m%d")
    all_headers = {"host": parts.netloc, "x-amz-date": stamp}
    if payload_hash is not None:
        all_headers["x-amz-content-sha256"] = payload_hash
    for name, value in (headers or {}).items():
        all_headers[name.lower()] = str(value).strip()
    names = sorted(all_headers)
    canonical_headers = "".join(f"{n}:{all_headers[n]}\n" for n in names)
    canonical_path = urllib.parse.quote(urllib.parse.unquote(parts.path) or "/", safe="/-_.~")
    query = "&".join(
        f"{_quote(k)}={_quote(v)}" for k, v in sorted(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    )
    # A request with no body is signed with the hash of nothing (and has no x-amz-content-sha256 header).
    body_hash = payload_hash if payload_hash is not None else hashlib.sha256(b"").hexdigest()
    canonical = "\n".join([method, canonical_path, query, canonical_headers, ";".join(names), body_hash])
    scope = f"{date}/{region}/{service}/aws4_request"
    to_sign = "\n".join([ALGORITHM, stamp, scope, hashlib.sha256(canonical.encode("utf-8")).hexdigest()])
    signature = hmac.new(signing_key(secret_key, date, region, service), to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    out = {n: all_headers[n] for n in names if n != "host"}
    out["Authorization"] = f"{ALGORITHM} Credential={access_key}/{scope}, SignedHeaders={';'.join(names)}, Signature={signature}"
    return out


# ---------- upload ----------
def object_key(path: Path) -> str:
    prefix = (getattr(settings, "BACKUP_S3_PREFIX", "") or "").strip("/")
    tin = (getattr(settings, "NODE_CLINIC_TIN", "") or "clinic").strip().replace("/", "_")
    return "/".join(p for p in (prefix, tin, path.name) if p)


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def upload(path: Path, now=None, timeout: int = 300) -> str:
    """PUT one encrypted backup. Returns the object key. Raises OffsiteError when it cannot be sent."""
    if not configured():
        raise OffsiteError("Off-site storage is not set up (BACKUP_S3_* settings).")
    if not backup_crypto.is_encrypted(path):
        raise OffsiteError("Refusing to upload an unencrypted backup. Set BACKUP_PASSPHRASE first.")
    now = now or dt.datetime.now(dt.timezone.utc)
    endpoint = settings.BACKUP_S3_ENDPOINT.rstrip("/")
    key = object_key(path)
    url = f"{endpoint}/{urllib.parse.quote(settings.BACKUP_S3_BUCKET)}/{urllib.parse.quote(key)}"
    size = path.stat().st_size
    headers = sign(
        "PUT", url,
        region=getattr(settings, "BACKUP_S3_REGION", "") or "us-east-1", service="s3",
        access_key=settings.BACKUP_S3_ACCESS_KEY, secret_key=settings.BACKUP_S3_SECRET_KEY,
        payload_hash=_sha256_of(path), now=now,
    )
    headers["Content-Length"] = str(size)
    headers["Content-Type"] = "application/octet-stream"
    with open(path, "rb") as body:
        request = urllib.request.Request(url, data=body, headers=headers, method="PUT")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if response.status not in (200, 201, 204):
                    raise OffsiteError(f"The storage service answered {response.status}.")
        except urllib.error.HTTPError as exc:
            raise OffsiteError(f"The storage service refused the upload ({exc.code}). Check the keys and the bucket name.") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OffsiteError("The storage service could not be reached. Check the internet connection.") from exc
    return key
