"""Limits and checks for untrusted files: size, archive traversal, decompression ratio, macros, XML entities."""

from __future__ import annotations

import posixpath
import re
import zipfile
from dataclasses import dataclass
from io import BytesIO

from wayam_connectors.contracts.errors import PolicyError, ValidationError

_ENTITY_RE = re.compile(rb"<!ENTITY\s", re.IGNORECASE)
_DOCTYPE_RE = re.compile(rb"<!DOCTYPE", re.IGNORECASE)
_MACRO_PARTS = ("vbaProject.bin", "vbaData.xml")
_ACTIVE_X = re.compile(r"^(word|ppt|xl)/activeX/", re.IGNORECASE)


@dataclass(frozen=True)
class FileLimits:
    max_bytes: int = 50 * 1024 * 1024
    max_uncompressed_bytes: int = 500 * 1024 * 1024
    max_compression_ratio: float = 100.0
    max_members: int = 10_000
    max_text_chars: int = 20_000_000


DEFAULT_LIMITS = FileLimits()


def check_size(data: bytes, limits: FileLimits = DEFAULT_LIMITS) -> None:
    if len(data) == 0:
        raise ValidationError("file is empty")
    if len(data) > limits.max_bytes:
        raise PolicyError("file exceeds size limit", details={"size": len(data), "max": limits.max_bytes})


def inspect_zip(data: bytes, limits: FileLimits = DEFAULT_LIMITS) -> zipfile.ZipFile:
    """Open any untrusted zip and reject traversal, too many members and decompression bombs."""
    return inspect_ooxml_zip(data, limits, scan_xml=False)


def inspect_ooxml_zip(
    data: bytes, limits: FileLimits = DEFAULT_LIMITS, *, allowed_prefixes: tuple[str, ...] = (), scan_xml: bool = True
) -> zipfile.ZipFile:
    """Open an OOXML container and reject traversal, bombs, macros, ActiveX and XML entity declarations.

    Returns an open ZipFile the parser may read from. Member XML is scanned for `<!ENTITY`/`<!DOCTYPE`
    before any XML parser touches it.
    """
    check_size(data, limits)
    try:
        zf = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValidationError("not a valid OOXML/zip container") from exc
    infos = zf.infolist()
    if len(infos) > limits.max_members:
        raise PolicyError("archive has too many members", details={"members": len(infos)})
    total = 0
    for info in infos:
        name = info.filename
        norm = posixpath.normpath(name)
        if name.startswith(("/", "\\")) or norm.startswith("..") or "\\" in name or ":" in name.split("/")[0]:
            raise PolicyError("archive member path traversal rejected", details={"member": name})
        if any(name.endswith(p) for p in _MACRO_PARTS) or _ACTIVE_X.match(name):
            raise PolicyError("macro/active content is not accepted", details={"member": name})
        total += info.file_size
        if info.compress_size and info.file_size / max(info.compress_size, 1) > limits.max_compression_ratio and info.file_size > 1_000_000:
            raise PolicyError("suspicious compression ratio", details={"member": name})
    if total > limits.max_uncompressed_bytes:
        raise PolicyError("archive expands beyond limit", details={"uncompressed": total})
    if allowed_prefixes and not any(i.filename.startswith(allowed_prefixes) for i in infos):
        raise ValidationError("container does not look like the expected Office document")
    for info in infos:
        if scan_xml and info.filename.lower().endswith((".xml", ".rels")):
            with zf.open(info) as fh:
                head = fh.read(min(info.file_size, 65536))
            if _ENTITY_RE.search(head) or _DOCTYPE_RE.search(head):
                raise PolicyError("XML DOCTYPE/ENTITY declarations are not accepted", details={"member": info.filename})
    return zf
