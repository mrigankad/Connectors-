from __future__ import annotations

import zipfile
from io import BytesIO

import pytest

from wayam_connectors.contracts.errors import PolicyError, ValidationError
from wayam_connectors.platform.security.files import FileLimits, inspect_ooxml_zip
from wayam_connectors.platform.security.redaction import redact, redact_headers, redact_text
from wayam_connectors.platform.security.ssrf import OriginPolicy


def test_redaction_of_keys_values_and_headers():
    out = redact(
        {
            "token": "abc",
            "nested": {"client_secret": "x", "ok": "fine"},
            "msg": "Authorization: Bearer ghp_abcdefghijklmnopqrstuvwxyz0123456789",
            "url": "https://x.example/a?X-Amz-Signature=abcdef1234567890&other=1",
            "list": ["AKIAABCDEFGHIJKLMNOP", 5],
            "pw": "password=hunter2&x=1",
        }
    )
    assert out["token"] == "[REDACTED]"
    assert out["nested"]["client_secret"] == "[REDACTED]" and out["nested"]["ok"] == "fine"
    assert "ghp_" not in out["msg"] and "[REDACTED]" in out["msg"]
    assert "abcdef1234567890" not in out["url"]
    assert out["list"][0] == "[REDACTED]" and out["list"][1] == 5
    assert "hunter2" not in out["pw"]
    assert redact_headers({"Authorization": "Bearer x", "Accept": "json"}) == {"Authorization": "[REDACTED]", "Accept": "json"}
    assert "PRIVATE KEY" not in redact_text("-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----")


def test_origin_policy_blocks_private_metadata_and_unlisted():
    pol = OriginPolicy.for_origins(["https://api.github.com"])
    assert pol.check_url("https://api.github.com/repos/x/y", resolve=False) == "https://api.github.com"
    for bad in [
        "http://api.github.com/x",  # scheme
        "https://evil.example/x",  # not allowlisted
        "https://user:pw@api.github.com/x",
        "https://169.254.169.254/latest/meta-data",
        "https://localhost/x",
        "https://metadata.google.internal/x",
        "https://10.0.0.1/x",
        "https://[::1]/x",
    ]:
        with pytest.raises(PolicyError):
            pol.check_url(bad, resolve=False)
    gw = OriginPolicy.for_origins(["http://10.0.0.5:8443"], allow_private=True)
    assert gw.check_url("http://10.0.0.5:8443/sap", resolve=False) == "http://10.0.0.5:8443"


def _zip(members: dict[str, bytes]) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_ooxml_inspection_rejects_attacks():
    with pytest.raises(ValidationError):
        inspect_ooxml_zip(b"")
    with pytest.raises(ValidationError):
        inspect_ooxml_zip(b"not a zip")
    with pytest.raises(PolicyError):
        inspect_ooxml_zip(_zip({"../evil.xml": b"<a/>", "word/document.xml": b"<w/>"}))
    with pytest.raises(PolicyError):
        inspect_ooxml_zip(_zip({"word/vbaProject.bin": b"\x00", "word/document.xml": b"<w/>"}))
    with pytest.raises(PolicyError):
        inspect_ooxml_zip(_zip({"word/document.xml": b'<!DOCTYPE x [<!ENTITY a "b">]><w/>'}))
    with pytest.raises(PolicyError):
        inspect_ooxml_zip(_zip({"word/document.xml": b"<w/>" + b"A" * 2_000_000}), FileLimits(max_compression_ratio=10))
    with pytest.raises(PolicyError):
        inspect_ooxml_zip(_zip({"word/document.xml": b"<w/>" * 100}), FileLimits(max_bytes=10))
    zf = inspect_ooxml_zip(_zip({"word/document.xml": b"<w/>"}), allowed_prefixes=("word/",))
    assert zf.namelist() == ["word/document.xml"]
    with pytest.raises(ValidationError):
        inspect_ooxml_zip(_zip({"ppt/slides/slide1.xml": b"<p/>"}), allowed_prefixes=("word/",))
