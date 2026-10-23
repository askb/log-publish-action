# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Bash compatible pattern matching for archive_patterns.

The shell version expanded these with bash under globstar, dotglob and
nullglob, and callers' patterns rely on that. The standard library
cannot reproduce it:

- glob.glob and Path.glob only match hidden files with include_hidden,
  which needs Python 3.11; the action supports 3.10.
- Both follow directory symlinks while expanding "**". A symlink in the
  build tree could then pull files from outside the workspace into the
  published logs. Bash globstar does not follow them, and neither does
  _match below.
- fnmatch lacks POSIX bracket classes such as [[:digit:]].

So each path component is translated to a regex that follows bash:

- "*" and "?" also match a leading dot (dotglob);
- "**" alone spans zero or more directories without following symlinks
  (globstar);
- a pattern that matches nothing expands to nothing (nullglob);
- bracket expressions support "!" or "^" negation, ranges, escapes and
  the POSIX classes of the C locale (ASCII);
- an unterminated "[" is a literal.

Keep these semantics, and the tests in TestGlob, when changing this
module. Reverting to glob.glob would reintroduce the symlink traversal.
"""

from __future__ import annotations

import functools
import os
import re
import string
from collections.abc import Iterator
from pathlib import Path

_MAGIC = frozenset("*?[")


# ASCII, as in the C locale the runners use.
_POSIX_CLASSES = {
    "alnum": "a-zA-Z0-9",
    "alpha": "a-zA-Z",
    "blank": " \\t",
    "cntrl": "\\x00-\\x1f\\x7f",
    "digit": "0-9",
    "graph": "!-~",
    "lower": "a-z",
    "print": " -~",
    "punct": re.escape(string.punctuation),
    "space": " \\t\\n\\r\\f\\v",
    "upper": "A-Z",
    "word": "a-zA-Z0-9_",
    "xdigit": "0-9A-Fa-f",
}


def _bracket(pattern: str, start: int) -> tuple[str, int] | None:
    """Translate the bracket expression at pattern[start] == "[".

    Returns the regex and the index after the closing "]", or None when
    the bracket is unterminated and so a literal "[", as in bash.
    """
    i = start + 1
    negate = i < len(pattern) and pattern[i] in "!^"
    if negate:
        i += 1
    items: list[str] = []
    first = True
    while i < len(pattern):
        ch = pattern[i]
        if ch == "]" and not first:
            body = "".join(items)
            if not body:
                return ("[^\\s\\S]" if not negate else "."), i + 1
            return f"[{'^' if negate else ''}{body}]", i + 1
        first = False
        if pattern.startswith("[:", i):
            end = pattern.find(":]", i + 2)
            if end != -1:
                items.append(_POSIX_CLASSES.get(pattern[i + 2 : end], ""))
                i = end + 2
                continue
        if ch == "\\" and i + 1 < len(pattern):
            i += 1
            ch = pattern[i]
        if pattern[i + 1 : i + 2] == "-" and pattern[i + 2 : i + 3] not in ("", "]"):
            high = pattern[i + 2]
            if ch <= high:
                items.append(f"{re.escape(ch)}-{re.escape(high)}")
            i += 3
            continue
        items.append(re.escape(ch))
        i += 1
    return None


@functools.lru_cache(maxsize=256)
def _component_regex(pattern: str) -> re.Pattern[str]:
    """A single path component of a bash pattern as a regex. * matches a
    leading dot (dotglob); bracket expressions accept POSIX classes."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            out.append(".*")
        elif ch == "?":
            out.append(".")
        elif ch == "[" and (bracket := _bracket(pattern, i)) is not None:
            out.append(bracket[0])
            i = bracket[1]
            continue
        elif ch == "\\" and i + 1 < len(pattern):
            i += 1
            out.append(re.escape(pattern[i]))
        else:
            out.append(re.escape(ch))
        i += 1
    return re.compile("".join(out), re.DOTALL)


def _has_magic(component: str) -> bool:
    return any(ch in _MAGIC for ch in component)


def _scandir(path: Path) -> list[os.DirEntry[str]]:
    try:
        with os.scandir(path) as entries:
            return sorted(entries, key=lambda e: e.name)
    except OSError:
        return []


def _walk_all(path: Path) -> Iterator[Path]:
    """Everything below path, without following directory symlinks."""
    for entry in _scandir(path):
        yield Path(entry.path)
        if entry.is_dir(follow_symlinks=False):
            yield from _walk_all(Path(entry.path))


def _match(path: Path, parts: list[str]) -> Iterator[Path]:
    if not parts:
        yield path
        return
    head, rest = parts[0], parts[1:]
    if head == "**":
        # Like bash globstar: zero or more directories, never following
        # a symlinked directory while recursing.
        if not rest:
            yield from _walk_all(path)
            return
        yield from _match(path, rest)
        for entry in _scandir(path):
            if entry.is_dir(follow_symlinks=False):
                yield from _match(Path(entry.path), parts)
    elif _has_magic(head):
        regex = _component_regex(head)
        for entry in _scandir(path):
            if regex.fullmatch(entry.name):
                yield from _match(Path(entry.path), rest)
    else:
        candidate = path / head
        if os.path.lexists(candidate):
            yield from _match(candidate, rest)


def glob_files(root: Path, pattern: str) -> list[Path]:
    """Files under root matching a bash globstar/dotglob/nullglob pattern."""
    if pattern.endswith("/"):
        return []
    parts = [part for part in pattern.split("/") if part]
    seen: dict[Path, None] = {}
    for match in _match(root, parts):
        if match.is_file():
            seen.setdefault(match, None)
    return list(seen)
