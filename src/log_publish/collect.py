# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Stage the files to publish into a temporary tree.

Collection, console capture and metadata generation are independent of
the destination; only the final upload stage differs.
"""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import string
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from datetime import datetime, timezone
from http.client import HTTPException, HTTPMessage, HTTPResponse
from pathlib import Path
from typing import IO, cast

from .config import Config
from .log import info, warn
from .paths import slugify

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


class Stager:
    """Copies files into the staging tree under their relative path."""

    def __init__(self, staging: Path) -> None:
        self.staging: Path = staging

    def stage(self, src: Path, rel: str) -> bool:
        dest = self.staging / rel
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            _ = shutil.copyfile(src, dest)
        except OSError as err:
            warn(f"Could not stage {rel}: {err.strerror or err}")
            return False
        return True

    def write(self, rel: str, text: str) -> None:
        dest = self.staging / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        _ = dest.write_text(text, encoding="utf-8")


def collect_archive_dir(stager: Stager, workspace: Path, archive_dir: str) -> None:
    """The conventional archives directory, copied wholesale."""
    base = workspace / archive_dir
    if not base.is_dir():
        return
    # os.walk follows a symlink given as its root, where find -type f
    # did not, so a linked archives directory could expose runner files.
    if base.is_symlink() or not _inside(base, workspace.resolve()):
        warn(f"Skipping {archive_dir}/: it leads outside the workspace")
        return
    info(f"Collecting {archive_dir}/ ...")
    for dirpath, _dirs, files in os.walk(base):
        for name in files:
            src = Path(dirpath) / name
            # find -type f: regular files, never symlinks.
            if src.is_symlink() or not src.is_file():
                continue
            _ = stager.stage(src, src.relative_to(base).as_posix())


def _inside(path: Path, root: Path) -> bool:
    try:
        _ = path.resolve().relative_to(root)
    except (OSError, ValueError):
        return False
    return True


def collect_patterns(stager: Stager, workspace: Path, patterns: str) -> None:
    """Explicit glob patterns, the JJB ARCHIVE_ARTIFACTS equivalent."""
    if not patterns:
        return
    info("Collecting archive_patterns ...")
    root = workspace.resolve()
    for raw in patterns.splitlines():
        pattern = raw.strip()
        if not pattern:
            continue
        if pattern.startswith("/") or ".." in pattern:
            warn(f"Ignoring unsafe pattern: {pattern}")
            continue
        matched = 0
        for src in glob_files(workspace, pattern):
            rel = src.relative_to(workspace).as_posix()
            # A symlink in the build tree must not publish a host file.
            if not _inside(src, root):
                warn(f"Skipping file outside the workspace: {rel}")
                continue
            if stager.stage(src, rel):
                matched += 1
        info(f"  {pattern} -> {matched} file(s)")


def build_details(cfg: Config) -> str:
    """The _build-details.log equivalent."""
    env = cfg.env
    server = env.get("GITHUB_SERVER_URL") or "https://github.com"
    repo = env.get("GITHUB_REPOSITORY", "")
    run_id = env.get("GITHUB_RUN_ID", "")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [
        ("build-url", f"{server}/{repo}/actions/runs/{run_id}"),
        ("repository", repo),
        ("workflow", env.get("GITHUB_WORKFLOW", "")),
        ("job", cfg.job_name),
        ("run-id", run_id),
        ("run-number", env.get("GITHUB_RUN_NUMBER", "")),
        ("run-attempt", env.get("GITHUB_RUN_ATTEMPT", "")),
        ("ref", env.get("GITHUB_REF", "")),
        ("sha", env.get("GITHUB_SHA", "")),
        ("event", env.get("GITHUB_EVENT_NAME", "")),
        ("actor", env.get("GITHUB_ACTOR", "")),
        ("runner-os", env.get("RUNNER_OS", "")),
        ("published", now),
    ]
    return "".join(f"{key}: {value}\n" for key, value in rows)


def _command_output(*argv: str) -> str:
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, check=False, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout


def sys_info() -> str:
    """The _sys-info.log equivalent."""
    sections = [
        ("uname", _command_output("uname", "-a")),
        ("cpu", _command_output("nproc")),
        ("memory", _command_output("free", "-h")),
        ("disk", _command_output("df", "-h")),
    ]
    return "\n".join(f"--- {name} ---\n{body}" for name, body in sections)


class StripAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """Drop the token when a redirect leaves the API host, and refuse
    any redirect away from https.

    The job log endpoint redirects to blob storage. urllib, unlike curl,
    would otherwise forward the Authorization header there.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise urllib.error.URLError("refusing a redirect away from https")
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            old_host = urllib.parse.urlsplit(req.full_url).netloc
            if urllib.parse.urlsplit(new.full_url).netloc != old_host:
                new.remove_header("Authorization")
        return new


def api_get(url: str, token: str, timeout: float = 60.0) -> bytes:
    if not url.startswith("https://"):
        raise ValueError("refusing a non https API URL")
    opener = urllib.request.build_opener(StripAuthOnRedirect())
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with cast(HTTPResponse, opener.open(request, timeout=timeout)) as response:
            return response.read()
    except HTTPException as err:
        # IncompleteRead and friends are not OSErrors; capture stays best
        # effort either way.
        raise OSError(f"{type(err).__name__}: {err}") from err


def _job_list(body: bytes) -> list[dict[str, object]]:
    data = cast(object, json.loads(body))
    if not isinstance(data, dict):
        raise ValueError("unexpected jobs response")
    jobs = cast(dict[str, object], data).get("jobs")
    if not isinstance(jobs, list):
        raise ValueError("unexpected jobs response")
    return [
        cast(dict[str, object], job)
        for job in cast(list[object], jobs)
        if isinstance(job, dict)
    ]


def capture_console(stager: Stager, cfg: Config) -> None:
    """Console logs of every job in this run, through the REST API.

    A job cannot read its own log while it is still running, which is why
    this belongs in a final job that depends on the others.
    """
    info("Capturing console logs ...")
    env = cfg.env
    api = env.get("GITHUB_API_URL") or "https://api.github.com"
    repo = env.get("GITHUB_REPOSITORY", "")
    run = env.get("GITHUB_RUN_ID", "")
    attempt = env.get("GITHUB_RUN_ATTEMPT") or "1"
    jobs_url = (
        f"{api}/repos/{repo}/actions/runs/{run}/attempts/{attempt}/jobs?per_page=100"
    )
    try:
        jobs = _job_list(api_get(jobs_url, cfg.github_token))
    except (OSError, ValueError):
        warn("Could not list jobs; console logs not captured")
        return
    for job in jobs:
        job_id, name = str(job.get("id", "")), str(job.get("name", ""))
        if not job_id:
            continue
        url = f"{api}/repos/{repo}/actions/jobs/{job_id}/logs"
        try:
            body = api_get(url, cfg.github_token)
        except (OSError, ValueError):
            warn(f"Could not capture console log for {name}")
            continue
        dest = stager.staging / "console" / f"{slugify(name)}.log"
        dest.parent.mkdir(parents=True, exist_ok=True)
        _ = dest.write_bytes(body)
        info(f"  captured {name}")


def collect(cfg: Config, staging: Path) -> None:
    """Populate staging with everything this run publishes."""
    stager = Stager(staging)
    workspace = Path(cfg.workspace)
    collect_archive_dir(stager, workspace, cfg.archive_dir)
    collect_patterns(stager, workspace, cfg.archive_patterns)
    stager.write("_build-details.log", build_details(cfg))
    if cfg.include_sysinfo == "true":
        stager.write("_sys-info.log", sys_info())
    if cfg.capture_console == "true" and not cfg.is_dry_run:
        capture_console(stager, cfg)
