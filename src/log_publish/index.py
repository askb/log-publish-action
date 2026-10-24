# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Directory listings for R2.

R2 has no equivalent of the S3 log server's listing Lambda, so render a
plain index.html for every directory of this run. A directory that
already ships its own index.html, such as an Allure report, keeps it.

Pages are written under flat numbered names, with pages.tsv mapping each
one to its object key. Mirroring the tree on disk would collide when a
collected directory is itself named index.html.
"""

from __future__ import annotations

from pathlib import Path

from .log import warn

_HTML = str.maketrans({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"})
# Browsers treat a backslash in an https URL as a slash, so encode it.
_HREF = str.maketrans(
    {
        "%": "%25",
        "\\": "%5C",
        " ": "%20",
        "#": "%23",
        "?": "%3F",
        "&": "&amp;",
        '"': "%22",
        "<": "%3C",
        ">": "%3E",
    }
)


def html_escape(text: str) -> str:
    return text.translate(_HTML)


def href_escape(text: str) -> str:
    return text.translate(_HREF)


def listing(keys: list[str]) -> dict[str, set[tuple[str, str]]]:
    """Map each directory ("" for the root, else "a/b/") to its children
    as (kind, name) pairs, kind "d" or "f"."""
    children: dict[str, set[tuple[str, str]]] = {}
    for key in keys:
        parts = key.split("/")
        parent = ""
        for i, part in enumerate(parts):
            kind = "d" if i < len(parts) - 1 else "f"
            children.setdefault(parent, set()).add((kind, part))
            parent = f"{parent}{part}/"
    return children


def render_page(s3_path: str, directory: str, entries: set[tuple[str, str]]) -> str:
    title = html_escape(f"/{s3_path}/{directory}")
    lines = [
        "<!DOCTYPE html>",
        '<html><head><meta charset="utf-8">',
        f"<title>Index of {title}</title></head><body>",
        f"<h1>Index of {title}</h1><ul>",
    ]
    if directory:
        lines.append('<li><a href="../">../</a></li>')
    for kind, name in sorted(entries):
        label = f"{name}/" if kind == "d" else name
        # The ./ prefix keeps a name such as javascript:x.log a relative
        # path rather than a URL scheme.
        lines.append(
            f'<li><a href="./{href_escape(label)}">{html_escape(label)}</a></li>'
        )
    lines.append("</ul></body></html>")
    return "\n".join(lines) + "\n"


def generate_index_pages(
    keys: list[str], s3_path: str, index_dir: Path
) -> list[tuple[Path, str]]:
    """Write one page per directory lacking an index.html.

    Returns (page file, object key relative to s3_path) pairs, also
    recorded in index_dir/pages.tsv for inspection after a dry run.
    """
    index_dir.mkdir(parents=True, exist_ok=True)
    existing = set(keys)
    pages: list[tuple[Path, str]] = []
    for directory, entries in sorted(listing(keys).items()):
        # Defence in depth: the manifest already excludes control
        # characters, but never let a row steer a page outside the tree.
        if "/../" in f"/{directory}" or "/./" in f"/{directory}":
            warn(f"Skipping index for unsafe directory: {directory}")
            continue
        key = f"{directory}index.html"
        if key in existing:
            continue
        page = index_dir / f"page-{len(pages) + 1}.html"
        _ = page.write_text(render_page(s3_path, directory, entries), encoding="utf-8")
        pages.append((page, key))
    tsv = "".join(f"{page}\t{key}\n" for page, key in pages)
    _ = (index_dir / "pages.tsv").write_text(tsv, encoding="utf-8")
    return pages
