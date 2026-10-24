# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Resolve the object key prefix a run publishes under."""

from __future__ import annotations

import re

from .config import Config

_UNSAFE = re.compile(r"[^a-z0-9._-]+")
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def slugify(value: str) -> str:
    """Reduce a string to one safe key segment.

    Matrix job names routinely contain spaces, slashes and parentheses,
    for example "15.1.x / publish-nexus"; left as they are, these would
    silently create extra path levels.
    """
    slug = _UNSAFE.sub("-", value.translate(_ASCII_LOWER)).strip("-")
    return slug[:100]


def trim_slashes(path: str) -> str:
    """Drop one leading and one trailing slash."""
    path = path[1:] if path.startswith("/") else path
    return path[:-1] if path.endswith("/") else path


def resolve_path(cfg: Config) -> str:
    """The s3_path override, or <prefix>/<ns>/<repo>/<workflow>/<run>/<attempt>/<job>."""
    if cfg.s3_path:
        return trim_slashes(cfg.s3_path)
    env = cfg.env
    repo = env.get("GITHUB_REPOSITORY", "").rsplit("/", 1)[-1]
    parts = [
        cfg.path_prefix,
        cfg.namespace,
        slugify(repo),
        slugify(env.get("GITHUB_WORKFLOW") or "workflow"),
        env.get("GITHUB_RUN_NUMBER") or "0",
        env.get("GITHUB_RUN_ATTEMPT") or "1",
        slugify(cfg.job_name),
    ]
    return "/".join(parts)
