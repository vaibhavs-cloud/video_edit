"""Full-quality delivery via R2 presigned links (export stage)."""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import pytest

from vedit.config import ExportCfg, R2Cfg, load_config
from vedit.stage import export

_CONFIG = Path(__file__).resolve().parents[2] / "config.yaml"


def _config():
    return load_config(_CONFIG)


def _cfg_on() -> object:
    return dataclasses.replace(_config(), export=_on_cfg())


@pytest.fixture()
def r2_env(monkeypatch):
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv(
        "R2_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    )
    return export.creds()


FIXED_NOW = datetime(2026, 10, 23, 15, 0, 0, tzinfo=timezone.utc)
ENDPOINT = "https://acc123.r2.cloudflarestorage.com"


def _on_cfg() -> ExportCfg:
    return ExportCfg(
        r2=R2Cfg(
            enabled=True,
            endpoint=ENDPOINT,
            bucket="vedit-exports",
            expires_h=72,
            teaser="video",
        )
    )


def test_creds_missing_when_env_unset(monkeypatch):
    monkeypatch.delenv("R2_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("R2_SECRET_ACCESS_KEY", raising=False)
    assert export.creds() == ("", "")


def test_config_parses_export_section_and_defaults_off():
    cfg = load_config(_CONFIG)
    assert cfg.export.r2.bucket == "vedit-exports"
    assert cfg.export.r2.endpoint.endswith(".r2.cloudflarestorage.com")
    assert cfg.export.r2.teaser in {"video", "document", "none"}
    assert not cfg.export.r2.enabled  # off until bucket + token exist


def test_enabled_gating(monkeypatch):
    monkeypatch.delenv("R2_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("R2_SECRET_ACCESS_KEY", raising=False)
    on = _cfg_on()
    assert not export.enabled(on)
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "a")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "s")
    assert export.enabled(on)
    assert not export.enabled(_config())


def test_presign_signs_only_host(r2_env):
    """Presigned GET must not require browsers to send x-amz-* headers."""
    url = export._presign_get(
        endpoint=ENDPOINT,
        bucket="vedit-exports",
        key="a1b2c3d4/final.mp4",
        expires=72 * 3600,
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
        "AKIDEXAMPLE/20261023/us-east-1/s3/aws4_request"
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
        f"GET\n{parsed.path}\n{canonical_query}\n"
        f"host:acc123.r2.cloudflarestorage.com\nhost\nUNSIGNED-PAYLOAD"
    )
    k_date = hmac.new(
        ("AWS4" + r2_env[1]).encode(), b"20261023", hashlib.sha256
    ).digest()
    k_region = hmac.new(k_date, b"us-east-1", hashlib.sha256).digest()
    k_service = hmac.new(k_region, b"s3", hashlib.sha256).digest()
    k_signing = hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            "20261023T150000Z",
            "20261023/us-east-1/s3/aws4_request",
            hashlib.sha256(canonical.encode()).hexdigest(),
        ]
    )
    expected = hmac.new(k_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()
    assert query["X-Amz-Signature"] == [expected]


def test_upload_headers(r2_env):
    headers = export._sign_headers(
        method="PUT",
        host="acc123.r2.cloudflarestorage.com",
        path="/vedit-exports/ref/final.mp4",
        extra_headers={"content-type": "video/mp4", "x-amz-date": "20261023T150000Z"},
        payload_hash="abc123",
        access=r2_env[0],
        secret=r2_env[1],
        now=FIXED_NOW,
    )
    auth = headers["Authorization"]
    assert auth.startswith(
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20261023/us-east-1/s3/aws4_request"
    )
    assert "SignedHeaders=content-type;x-amz-content-sha256;x-amz-date" in auth
    assert headers["x-amz-content-sha256"] == "abc123"


def test_presign_url_requires_creds(monkeypatch):
    monkeypatch.delenv("R2_ACCESS_KEY_ID", raising=False)
    with pytest.raises(export.ExportError):
        export.presign_url(endpoint=ENDPOINT, bucket="b", key="k", expires=60)


def test_upload_rejects_4xx(r2_env, monkeypatch):
    class FakeResp:
        status_code = 403
        text = "denied"

    class FakeClient:
        def __init__(self, *a, **kw):
            self.timeout = kw.get("timeout", None)

        def put(self, url, headers=None, content=None):
            lower = {k.lower(): v for k, v in (headers or {}).items()}
            assert lower["authorization"].startswith("AWS4-HMAC-SHA256")
            for chunk in content:
                pass
            return FakeResp()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(export.httpx, "Client", FakeClient)
    src = Path("/tmp/final.mp4")
    src.parent.mkdir(exist_ok=True)
    src.write_bytes(b"x")
    with pytest.raises(export.ExportError):
        export._upload(endpoint=ENDPOINT, bucket="b", key="k/final.mp4", src=src)


def test_upload_retries_5xx(r2_env, monkeypatch):
    calls = []

    class FakeResp:
        status_code = 503
        text = ""

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        def put(self, url, headers=None, content=None):
            calls.append(1)
            return FakeResp()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(export.httpx, "Client", FakeClient)
    monkeypatch.setattr(export.time, "sleep", lambda s: None)
    src = Path("/tmp/final.mp4")
    src.write_bytes(b"x")
    with pytest.raises(export.ExportError):
        export._upload(endpoint=ENDPOINT, bucket="b", key="k/final.mp4", src=src)
    assert len(calls) == 3
