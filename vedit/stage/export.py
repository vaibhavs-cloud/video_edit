"""R2/S3 export: upload the final render and hand back a presigned link.

Telegram caps video uploads at 50MB (config: limits.telegram_send_max_bytes)
and re-encodes whatever it does accept, so full quality goes to a private R2
bucket with a time-limited presigned GET link instead. Everything needed for
that (upload + link) happens here with stdlib only — no boto3, no rclone:

- credentials come from env R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY (use-multi)
- endpoint/bucket/expiry knobs live in config.yaml export.r2 (never in code)
- signing is AWS SigV4 (R2 is S3-compatible, region already us-east-1)
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
import urllib.parse
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
    r2 = getattr(getattr(cfg, "export", None), "r2", None)
    return bool(r2) and bool(r2.enabled) and all(creds())


def _host(endpoint: str) -> str:
    return urllib.parse.urlparse(endpoint).netloc


def _path(bucket: str, key: str) -> str:
    return f"/{bucket}/{key}"


def _sign_headers(
    *,
    method: str,
    host: str,
    path: str,
    extra_headers: dict[str, str],
    payload_hash: str,
    access: str,
    secret: str,
    now: datetime | None = None,
) -> dict[str, str]:
    """SigV4 Authorization headers for the direct upload PUT."""
    now = now or datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    scope = f"{amz_date[:8]}/us-east-1/s3/aws4_request"
    headers = {
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
        **extra_headers,
    }
    signed = ";".join(sorted(headers))
    canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
    canonical = f"{method}\n{path}\n\n{canonical_headers}\n{signed}\n{payload_hash}"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical.encode()).hexdigest(),
        ]
    )
    signing_key = _hmac(("AWS4" + secret).encode(), amz_date[:8])
    signing_key = _hmac(signing_key, "us-east-1")
    signing_key = _hmac(signing_key, "s3")
    signing_key = _hmac(signing_key, "aws4_request")
    signature = hmac.new(
        signing_key, string_to_sign.encode(), hashlib.sha256
    ).hexdigest()
    return {
        **headers,
        "Authorization": (
            f"AWS4-HMAC-SHA256 Credential={access}/{scope}, "
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
    now: datetime | None = None,
) -> str:
    """Time-limited GET link for a stored object — signs only the host header
    so plain browsers/apps can open it without sending any x-amz headers."""
    now = now or datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    scope = f"{amz_date[:8]}/us-east-1/s3/aws4_request"
    host = _host(endpoint)
    query = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{access}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(expires),
        "X-Amz-SignedHeaders": "host",
    }
    canonical_query = "&".join(
        f"{urllib.parse.quote(k, safe='-_.~')}="
        f"{urllib.parse.quote(query[k], safe='-_.~')}"
        for k in sorted(query)
    )
    canonical = "\n".join(
        [
            "GET",
            _path(bucket, key),
            canonical_query,
            f"host:{host}",
            "host",
            "UNSIGNED-PAYLOAD",
        ]
    )
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical.encode()).hexdigest(),
        ]
    )
    signing_key = _hmac(("AWS4" + secret).encode(), amz_date[:8])
    signing_key = _hmac(signing_key, "us-east-1")
    signing_key = _hmac(signing_key, "s3")
    signing_key = _hmac(signing_key, "aws4_request")
    signature = hmac.new(
        signing_key, string_to_sign.encode(), hashlib.sha256
    ).hexdigest()
    return (
        f"{endpoint}{_path(bucket, key)}?{canonical_query}&X-Amz-Signature={signature}"
    )


def presign_url(*, endpoint: str, bucket: str, key: str, expires: int) -> str:
    access, secret = creds()
    if not access or not secret:
        raise ExportError("R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY are not set")
    return _presign_get(
        endpoint=endpoint.rstrip("/"),
        bucket=bucket,
        key=key,
        expires=max(60, expires),
        access=access,
        secret=secret,
    )


def _upload(*, endpoint: str, bucket: str, key: str, src: Path) -> None:
    access, secret = creds()
    if not src.exists():
        raise ExportError(f"missing export source: {src}")
    size = src.stat().st_size
    if size <= 0:
        raise ExportError(f"empty export source: {src}")
    endpoint = endpoint.rstrip("/")
    sha = hashlib.sha256()
    with src.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            sha.update(chunk)
    url = f"{endpoint}{_path(bucket, key)}"
    last: Exception | None = None
    for attempt in range(3):
        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        headers = _sign_headers(
            method="PUT",
            host=_host(endpoint),
            path=_path(bucket, key),
            extra_headers={"content-type": "video/mp4", "x-amz-date": amz_date},
            payload_hash=sha.hexdigest(),
            access=access,
            secret=secret,
        )
        try:
            with httpx.Client(
                timeout=httpx.Timeout(1800.0), follow_redirects=True
            ) as client:
                with src.open("rb") as fh:
                    resp = client.put(url, headers=headers, content=fh)
                if resp.status_code < 300:
                    return
                if resp.status_code < 500:
                    raise ExportError(
                        f"r2 upload rejected: {resp.status_code} {resp.text[:200]}"
                    )
                last = ExportError(f"r2 {resp.status_code}")
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            last = exc
        if attempt < 2:
            time.sleep(min(2**attempt, 8))
    assert last is not None
    raise last


def upload_and_presign(cfg, state: Path, ref: str) -> str:
    """Upload final.mp4 and return the presigned link. Raises ExportError."""
    r2 = cfg.export.r2
    if not creds()[0]:
        raise ExportError("R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY are not set")
    key = f"{ref}/final.mp4"
    _upload(endpoint=r2.endpoint, bucket=r2.bucket, key=key, src=state / "final.mp4")
    return presign_url(
        endpoint=r2.endpoint, bucket=r2.bucket, key=key, expires=r2.expires_h * 3600
    )
