# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Compress the staged tree and describe every object to upload.

Two deliberate corrections to the Jenkins behaviour live here:

1. Compressed files keep their original object key. Jenkins renames
   log.html to log.html.gz, which breaks every relative link inside a
   multi file report. Setting Content-Encoding instead preserves the
   links and still saves the bytes.
2. Content types come from an explicit table rather than the mimetypes
   database, whose answers vary by interpreter version and which
   collapses css, js and json to text/plain.
"""

from __future__ import annotations

import gzip
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .log import warn

# Worth compressing. Binary and already compressed formats are left
# alone; gzipping them wastes CPU and can inflate the result.
COMPRESS_EXTENSIONS = frozenset(
    "html htm log txt xml json css js csv md yaml yml svg".split()
)

CONTENT_TYPES = {
    "html": "text/html",
    "htm": "text/html",
    "css": "text/css",
    "js": "text/javascript",
    "mjs": "text/javascript",
    "json": "application/json",
    "xml": "application/xml",
    "svg": "image/svg+xml",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "ico": "image/vnd.microsoft.icon",
    "webp": "image/webp",
    "woff": "font/woff",
    "woff2": "font/woff2",
    "ttf": "font/ttf",
    "pdf": "application/pdf",
    "csv": "text/csv",
    "md": "text/markdown",
    "yaml": "application/yaml",
    "yml": "application/yaml",
    "zip": "application/zip",
    "jar": "application/java-archive",
    "tar": "application/x-tar",
    "gz": "application/gzip",
    "tgz": "application/gzip",
}


def _extension(name: str) -> str:
    base = name.rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[-1].lower() if "." in base else ""


def content_type_for(name: str) -> str:
    """Everything unlisted, including the extensionless and .log files
    that dominate build output, is readable text."""
    return CONTENT_TYPES.get(_extension(name), "text/plain")


def should_compress(name: str) -> bool:
    return _extension(name) in COMPRESS_EXTENSIONS


@dataclass(frozen=True)
class Entry:
    """One staged file: its path, object key, type, encoding and size."""

    rel: str
    key: str
    content_type: str
    encoding: str
    size: int

    def row(self) -> str:
        return "\t".join(
            (
                self.rel,
                self.key,
                self.content_type,
                self.encoding or "-",
                str(self.size),
            )
        )


def _has_control(text: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text)


def _gzip_in_place(path: Path) -> bool:
    tmp = path.with_name(path.name + ".lpa-gz")
    try:
        with path.open("rb") as src, tmp.open("wb") as raw:
            # mtime=0 keeps the output reproducible.
            with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=9, mtime=0) as gz:
                shutil.copyfileobj(src, gz, 1024 * 1024)
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        return False
    return True


def _staged_files(staging: Path) -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []
    for dirpath, _dirs, files in os.walk(staging):
        for name in files:
            path = Path(dirpath) / name
            found.append((path.relative_to(staging).as_posix(), path))
    return sorted(found)


def build_manifest(staging: Path, compress: bool, compress_suffix: bool) -> list[Entry]:
    """Compress eligible files in place and return the sorted manifest."""
    entries: list[Entry] = []
    for rel, path in _staged_files(staging):
        # The manifest is tab separated and line based, and keys must be
        # valid UTF-8. A control character could corrupt the manifest and
        # steer later stages to the wrong key; such names are never
        # legitimate build output, so drop them.
        if _has_control(rel):
            warn(f"Skipping file with control characters: {rel!r}")
            path.unlink(missing_ok=True)
            continue
        try:
            _ = rel.encode("utf-8")
        except UnicodeEncodeError:
            warn(f"Skipping file with a name that is not UTF-8: {rel!r}")
            path.unlink(missing_ok=True)
            continue
        encoding = ""
        key = rel
        if compress and should_compress(rel) and _gzip_in_place(path):
            encoding = "gzip"
            # Legacy Jenkins naming, off by default: renaming the object
            # breaks relative links between report pages.
            if compress_suffix:
                key = f"{rel}.gz"
        # The type always reflects the original name. A gzipped log.html
        # is still text/html; Content-Encoding describes the compression.
        entries.append(
            Entry(rel, key, content_type_for(rel), encoding, path.stat().st_size)
        )
    return entries
