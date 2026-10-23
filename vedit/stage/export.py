"""S3-compatible export: upload the final render and hand back a presigned link.

Telegram caps video uploads at 50MB (config: limits.telegram_send_max_bytes)
and re-encodes whatever it does accept, so full quality goes to a private
S3-compatible bucket (Backblaze B2 by default, Cloudflare R2 also works) with
a time-limited presigned GET link instead. Everything needed for that (upload
+ link) happens here with stdlib only — no boto3, no rclone:

- credentials come from env R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY (the env
  names are kept generic across providers)
- endpoint/bucket/region/expiry knobs live in config.yaml export.s3 (never in
  code); B2 expects region to match the bucket's endpoint region
- signing is AWS SigV4 (service s3); B2 additionally requires the host header
  to be signed and R2 is byte-identical with AWS's SigV4 test vectors

A rolling storage budget rides along (export.s3.notify_gb / evict_gb): every
delivery, the bucket is listed; a Telegram nudge fires at notify_gb and the
least-recently-modified exports are deleted until total drops under notify_gb
once the evict_gb ceiling is hit. B2 has no read-time tracking, so
"last accessed" means "least recently uploaded".
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import httpx


class ExportError(RuntimeError):
    pass


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def creds() -> tuple[str, str]:
    """(access key id, secret key) from the environment — empty if unset."""
    return (
        os.environ.get("R2_ACCESS_KEY_ID", ""),
        os.environ.get("R2_SECRET_ACCESS_KEY", ""),
    )


def enabled(cfg) -> bool:
    s3 = getattr(getattr(cfg, "export", None), "s3", None)
    return (
        bool(s3)
        and bool(s3.enabled)
        and bool(getattr(s3, "endpoint", ""))
        and all(creds())
    )


def _derive_signing_key(secret: str, date_s: str, region: str) -> bytes:
    key = _hmac(("AWS4" + secret).encode(), date_s)
    key = _hmac(key, region)
    key = _hmac(key, "s3")
    return _hmac(key, "aws4_request")


def _host(endpoint: str) -> str:
    return urllib.parse.urlparse(endpoint).netloc


def _path(bucket: str, key: str = "") -> str:
    return f"/{bucket}/{key}" if key else f"/{bucket}"


def _enc(value: str) -> str:
    return urllib.parse.quote(value, safe="-_.~")


def _canonical_query(query: dict[str, str]) -> str:
    return "&".join(f"{_enc(k)}={_enc(query[k])}" for k in sorted(query))


def _sign_headers(
    *,
    method: str,
    host: str,
    path: str,
    canonical_query: str = "",
    extra_headers: dict[str, str] | None = None,
    payload_hash: str,
    access: str,
    secret: str,
    region: str,
    now: datetime | None = None,
) -> dict[str, str]:
    """SigV4 headers for a signed request of any S3 method."""
    now = now or datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    headers = {
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
        **(extra_headers or {}),
    }
    signed = ";".join(sorted(headers))
    canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
    canonical = f"{method}\n{path}\n{canonical_query}\n{canonical_headers}\n{signed}\n{payload_hash}"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            f"{amz_date[:8]}/{region}/s3/aws4_request",
            hashlib.sha256(canonical.encode()).hexdigest(),
        ]
    )
    signing_key = _derive_signing_key(secret, amz_date[:8], region)
    signature = hmac.new(
        signing_key, string_to_sign.encode(), hashlib.sha256
    ).hexdigest()
    return {
        **headers,
        "Authorization": (
            f"AWS4-HMAC-SHA256 Credential={access}/{amz_date[:8]}/{region}/s3/aws4_request, "
            f"SignedHeaders={signed}, Signature={signature}"
        ),
    }


def _presign_get(
    *,
    endpoint: str,
    bucket: str,
    key: str,
    expires: int,
    access: str,
    secret: str,
    region: str,
    now: datetime | None = None,
) -> str:
    """Time-limited GET link for a stored object — signs only the host header
    so plain browsers/apps can open it without sending any x-amz headers."""
    now = now or datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    scope = f"{amz_date[:8]}/{region}/s3/aws4_request"
    host = _host(endpoint)
    query = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{access}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(expires),
        "X-Amz-SignedHeaders": "host",
    }
    canonical_query = _canonical_query(query)
    canonical = (
        f"GET\n{_path(bucket, key)}\n{canonical_query}\nhost:{host}\n\n"
        f"host\nUNSIGNED-PAYLOAD"
    )
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical.encode()).hexdigest(),
        ]
    )
    signing_key = _derive_signing_key(secret, amz_date[:8], region)
    signature = hmac.new(
        signing_key, string_to_sign.encode(), hashlib.sha256
    ).hexdigest()
    return (
        f"{endpoint}{_path(bucket, key)}?{canonical_query}&X-Amz-Signature={signature}"
    )


def presign_url(
    *, endpoint: str, bucket: str, key: str, expires: int, region: str
) -> str:
    access, secret = creds()
    if not access or not secret:
        raise ExportError("R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY are not set")
    return _presign_get(
        endpoint=endpoint.rstrip("/"),
        bucket=bucket,
        key=key,
        expires=max(60, expires),
        region=region,
        access=access,
        secret=secret,
    )


def _request(
    *,
    method: str,
    endpoint: str,
    bucket: str,
    key: str = "",
    query: dict[str, str] | None = None,
    payload: bytes = b"",
    content_type: str = "",
    region: str,
) -> httpx.Response:
    """Signed S3 request with transient-failure retries. Raises ExportError 4xx."""
    access, secret = creds()
    if not all(creds()):
        raise ExportError("R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY are not set")
    endpoint = endpoint.rstrip("/")
    sha = hashlib.sha256(payload).hexdigest()
    cq = _canonical_query(query or {})
    url = f"{endpoint}{_path(bucket, key)}" + (f"?{cq}" if cq else "")
    last: Exception | None = None
    for attempt in range(3):
        amz_date = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        headers = _sign_headers(
            method=method,
            host=_host(endpoint),
            path=_path(bucket, key),
            canonical_query=cq,
            extra_headers={
                **({"content-type": content_type} if content_type else {}),
                "x-amz-date": amz_date,
            },
            payload_hash=sha,
            access=access,
            secret=secret,
            region=region,
        )
        try:
            with httpx.Client(
                timeout=httpx.Timeout(1800.0), follow_redirects=True
            ) as client:
                if method == "PUT":
                    resp = client.put(url, headers=headers, content=payload)
                elif method == "DELETE":
                    resp = client.delete(url, headers=headers)
                else:
                    resp = client.get(url, headers=headers)
            if resp.status_code < 300:
                return resp
            if resp.status_code < 500:
                raise ExportError(
                    f"{method} rejected: {resp.status_code} {resp.text[:200]}"
                )
            last = ExportError(f"{method} {resp.status_code}")
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            last = exc
        if attempt < 2:
            time.sleep(min(2**attempt, 8))
    assert last is not None
    raise last


def _upload(*, endpoint: str, bucket: str, key: str, src: Path, region: str) -> None:
    if not src.exists():
        raise ExportError(f"missing export source: {src}")
    size = src.stat().st_size
    if size <= 0:
        raise ExportError(f"empty export source: {src}")
    payload = src.read_bytes()
    _request(
        method="PUT",
        endpoint=endpoint,
        bucket=bucket,
        key=key,
        payload=payload,
        content_type="video/mp4",
        region=region,
    )


def list_objects(*, endpoint: str, bucket: str, region: str) -> list[dict]:
    """Every object in the bucket: {key, size, last} (B2 caps pages at 1000)."""
    out: list[dict] = []
    token = ""
    while True:
        query = {"list-type": "2"}
        if token:
            query["continuation-token"] = token
        resp = _request(
            method="GET",
            endpoint=endpoint,
            bucket=bucket,
            query=query,
            region=region,
        )
        root = ET.fromstring(resp.content)
        ns = root.tag.split("}")[0] + "}" if "}" in root.tag else ""
        for c in root.findall(f"{ns}Contents"):
            out.append(
                {
                    "key": c.findtext(f"{ns}Key", ""),
                    "size": int(c.findtext(f"{ns}Size", "0")),
                    "last": c.findtext(f"{ns}LastModified", ""),
                }
            )
        token = ""
        trunc = root.findtext(f"{ns}IsTruncated", "false") == "true"
        if trunc:
            token = root.findtext(f"{ns}NextContinuationToken", "") or ""
        if not trunc or not token:
            return out


def delete_object(*, endpoint: str, bucket: str, key: str, region: str) -> None:
    _request(method="DELETE", endpoint=endpoint, bucket=bucket, key=key, region=region)


def maintenance(cfg) -> tuple[int, list[str]]:
    """Rolling storage budget. Returns (total_bytes_after, deleted_keys).

    - usage >= notify_gb: caller should nudge via Telegram
    - usage >= evict_gb: least-recently-modified exports are deleted until
      usage drops to notify_gb
    """
    s3 = cfg.export.s3
    if not all(creds()):
        raise ExportError("R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY are not set")
    objects = list_objects(endpoint=s3.endpoint, bucket=s3.bucket, region=s3.region)
    total = sum(o["size"] for o in objects)
    deleted: list[str] = []
    evict_bytes = int(s3.evict_gb * (1 << 30))
    notify_bytes = int(s3.notify_gb * (1 << 30))
    if total >= evict_bytes:
        by_age = sorted(objects, key=lambda o: o["last"])
        while by_age and total >= notify_bytes:
            victim = by_age.pop(0)
            delete_object(
                endpoint=s3.endpoint,
                bucket=s3.bucket,
                key=victim["key"],
                region=s3.region,
            )
            total -= victim["size"]
            deleted.append(victim["key"])
    return total, deleted


def upload_and_presign(cfg, state: Path, ref: str) -> str:
    """Upload final.mp4 and return the presigned link. Raises ExportError."""
    s3 = cfg.export.s3
    if not all(creds()):
        raise ExportError("R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY are not set")
    key = f"{ref}/final.mp4"
    _upload(
        endpoint=s3.endpoint,
        bucket=s3.bucket,
        key=key,
        src=state / "final.mp4",
        region=s3.region,
    )
    return presign_url(
        endpoint=s3.endpoint,
        bucket=s3.bucket,
        key=key,
        expires=s3.expires_h * 3600,
        region=s3.region,
    )
