"""S3-compatible full-quality delivery via presigned links (export stage)."""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import pytest

from vedit.config import ExportCfg, S3Cfg, load_config
from vedit.stage import export

_CONFIG = Path(__file__).resolve().parents[2] / "config.yaml"
R2_HOST = "acc123.r2.cloudflarestorage.com"
ENDPOINT = f"https://{R2_HOST}"
REGION = "us-west-004"
FIXED_NOW = datetime(2026, 10, 23, 15, 0, 0, tzinfo=timezone.utc)


def _config():
    return load_config(_CONFIG)


def _cfg_on():
    return dataclasses.replace(_config(), export=_export_on())


def _export_on() -> ExportCfg:
    return ExportCfg(
        s3=S3Cfg(
            enabled=True,
            endpoint=ENDPOINT,
            bucket="vedit-exports",
            region=REGION,
            expires_h=72,
            teaser="video",
        )
    )


@pytest.fixture()
def r2_env(monkeypatch):
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv(
        "R2_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    )
    return export.creds()


def test_creds_missing_when_env_unset(monkeypatch):
    monkeypatch.delenv("R2_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("R2_SECRET_ACCESS_KEY", raising=False)
    assert export.creds() == ("", "")


def test_config_parses_export_section_and_defaults_off():
    cfg = _config()
    assert cfg.export.s3.bucket == "vedit-exports"
    assert cfg.export.s3.teaser in {"video", "document", "none"}
    assert cfg.export.s3.enabled  # on: bucket + app key exist


def test_enabled_gating(monkeypatch):
    monkeypatch.delenv("R2_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("R2_SECRET_ACCESS_KEY", raising=False)
    on = _cfg_on()
    assert not export.enabled(on)  # no creds
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "a")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "s")
    assert export.enabled(on)
    assert export.enabled(_config())  # repo config has live B2 creds path


def test_presign_signs_only_host(r2_env):
    """Presigned GET must not require browsers to send x-amz-* headers."""
    url = export._presign_get(
        endpoint=ENDPOINT,
        bucket="vedit-exports",
        key="a1b2c3d4/final.mp4",
        expires=72 * 3600,
        region=REGION,
        access=r2_env[0],
        secret=r2_env[1],
        now=FIXED_NOW,
    )
    parsed = urllib.parse.urlparse(url)
    assert parsed.path == "/vedit-exports/a1b2c3d4/final.mp4"
    query = urllib.parse.parse_qs(parsed.query)
    assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert query["X-Amz-Expires"] == [str(72 * 3600)]
    assert query["X-Amz-SignedHeaders"] == ["host"]
    assert query["X-Amz-Credential"] == [
        f"AKIDEXAMPLE/20261023/{REGION}/s3/aws4_request"
    ]

    canonical_query = "&".join(
        f"{k}={urllib.parse.quote(query[k][0], safe='-_.~')}"
        for k in (
            "X-Amz-Algorithm",
            "X-Amz-Credential",
            "X-Amz-Date",
            "X-Amz-Expires",
            "X-Amz-SignedHeaders",
        )
    )
    canonical = (
        f"GET\n{parsed.path}\n{canonical_query}\nhost:{R2_HOST}\n\n"
        f"host\nUNSIGNED-PAYLOAD"
    )
    k_date = hmac.new(
        ("AWS4" + r2_env[1]).encode(), b"20261023", hashlib.sha256
    ).digest()
    k_region = hmac.new(k_date, REGION.encode(), hashlib.sha256).digest()
    k_service = hmac.new(k_region, b"s3", hashlib.sha256).digest()
    k_signing = hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            "20261023T150000Z",
            f"20261023/{REGION}/s3/aws4_request",
            hashlib.sha256(canonical.encode()).hexdigest(),
        ]
    )
    expected = hmac.new(k_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()
    assert query["X-Amz-Signature"] == [expected]


def test_sign_headers_includes_host(r2_env):
    headers = export._sign_headers(
        method="PUT",
        host=R2_HOST,
        path="/vedit-exports/ref/final.mp4",
        extra_headers={"content-type": "video/mp4", "x-amz-date": "20261023T150000Z"},
        payload_hash="abc123",
        access=r2_env[0],
        secret=r2_env[1],
        now=FIXED_NOW,
        region=REGION,
    )
    auth = headers["Authorization"]
    assert auth.startswith(
        f"AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20261023/{REGION}/s3/aws4_request"
    )
    assert "SignedHeaders=content-type;host;x-amz-content-sha256;x-amz-date" in auth
    assert headers["host"] == R2_HOST
    assert headers["x-amz-content-sha256"] == "abc123"


def test_presign_url_requires_creds(monkeypatch, tmp_path):
    monkeypatch.delenv("R2_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("R2_SECRET_ACCESS_KEY", raising=False)
    with pytest.raises(export.ExportError):
        export.presign_url(
            endpoint=ENDPOINT, bucket="b", key="k", expires=60, region=REGION
        )


def _fake_client(monkeypatch, responses):
    """Swap httpx.Client with a canned responder; returns recorded calls."""
    calls = []

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        def put(self, url, headers=None, content=None):
            calls.append(
                {
                    "url": url,
                    "headers": headers,
                    "body": content
                    if isinstance(content, (bytes, bytearray))
                    else b"".join(content),
                }
            )
            return responses[len(calls) - 1] if len(responses) > 1 else responses[0]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(export.httpx, "Client", FakeClient)
    monkeypatch.setattr(export.time, "sleep", lambda s: None)
    return calls


def _make_src(tmp_path):
    src = tmp_path / "final.mp4"
    src.write_bytes(b"videobytes")
    return src


def test_upload_success(r2_env, monkeypatch, tmp_path):
    calls = _fake_client(monkeypatch, [_Ok()])
    export._upload(
        endpoint=ENDPOINT,
        bucket="b",
        key="k/final.mp4",
        src=_make_src(tmp_path),
        region=REGION,
    )
    assert calls[0]["url"] == f"{ENDPOINT}/b/k/final.mp4"
    assert (
        calls[0]["headers"]["x-amz-content-sha256"]
        == hashlib.sha256(b"videobytes").hexdigest()
    )


class _Ok:
    status_code = 200
    text = ""


class _Err:
    def __init__(self, code):
        self.status_code = code
        self.text = "nope"


def test_upload_rejects_4xx(r2_env, monkeypatch, tmp_path):
    calls = _fake_client(monkeypatch, [_Err(403)])
    with pytest.raises(export.ExportError):
        export._upload(
            endpoint=ENDPOINT,
            bucket="b",
            key="k/final.mp4",
            src=_make_src(tmp_path),
            region=REGION,
        )
    assert len(calls) == 1


def test_upload_retries_5xx(r2_env, monkeypatch, tmp_path):
    calls = _fake_client(monkeypatch, [_Err(503)])
    with pytest.raises(export.ExportError):
        export._upload(
            endpoint=ENDPOINT,
            bucket="b",
            key="k/final.mp4",
            src=_make_src(tmp_path),
            region=REGION,
        )
    assert len(calls) == 3


def test_upload_roundtrip_and_presign(r2_env, monkeypatch, tmp_path):
    uploads = []

    def fake_upload(**kwargs):
        uploads.append(kwargs)

    monkeypatch.setattr(export, "_upload", fake_upload)
    cfg = _cfg_on()
    link = export.upload_and_presign(cfg, tmp_path, "a1b2c3d4")
    assert uploads and uploads[0]["key"] == "a1b2c3d4/final.mp4"
    assert uploads[0]["endpoint"] == ENDPOINT
    assert uploads[0]["region"] == REGION
    assert link.startswith(f"{ENDPOINT}/vedit-exports/a1b2c3d4/final.mp4?")
    assert "X-Amz-Signature=" in link
