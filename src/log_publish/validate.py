# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Validate the action inputs.

Runs as its own composite step so that misconfiguration fails loudly and
immediately, rather than being swallowed by permit_fail while publishing.
"""

from __future__ import annotations

import importlib.util
import re

from .config import VALID_DESTINATIONS, Config, split_destinations
from .log import info
from .paths import trim_slashes

S3_BUCKET_RE = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
R2_BUCKET_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]")
R2_ACCOUNT_RE = re.compile(r"[0-9a-f]{32}")


class ValidationError(Exception):
    """An input is unusable. The first line is the error, the rest hints."""

    def __init__(self, message: str, *hints: str) -> None:
        super().__init__(message)
        self.message: str = message
        self.hints: tuple[str, ...] = hints


def _pip_available() -> bool:
    return importlib.util.find_spec("pip") is not None


def validate(cfg: Config, pip_available: bool | None = None) -> None:
    """Raise ValidationError for the first problem found."""
    members = split_destinations(cfg.destination)
    if not members or any(m not in VALID_DESTINATIONS for m in members):
        raise ValidationError(
            "destination must list s3, r2, artifact or both ❌",
            f"Received: '{cfg.destination}'",
        )
    dest = cfg.destinations
    dry_run = cfg.dry_run == "true"

    # The S3 checks apply only when something is going to S3. An
    # artifact only run needs no bucket and no credentials.
    if dest.s3 and not cfg.s3_bucket:
        raise ValidationError(
            f"s3_bucket is required when destination is '{cfg.destination}' ❌"
        )
    if cfg.s3_bucket and not S3_BUCKET_RE.fullmatch(cfg.s3_bucket):
        raise ValidationError(
            f"s3_bucket '{cfg.s3_bucket}' is not a valid bucket name ❌"
        )

    if dest.r2:
        if not cfg.r2_bucket:
            raise ValidationError(
                "r2_bucket is required when destination includes r2 ❌"
            )
        if not R2_BUCKET_RE.fullmatch(cfg.r2_bucket):
            raise ValidationError(
                f"r2_bucket '{cfg.r2_bucket}' is not a valid bucket name ❌"
            )
        if cfg.r2_endpoint_url:
            if not cfg.r2_endpoint_url.startswith("https://"):
                raise ValidationError("r2_endpoint_url must be an https URL ❌")
        elif not R2_ACCOUNT_RE.fullmatch(cfg.r2_account_id):
            raise ValidationError(
                "r2_account_id (32 hex characters) or r2_endpoint_url is"
                + " required when destination includes r2 ❌"
            )
        if not dry_run and not (cfg.r2_access_key_id and cfg.r2_secret_access_key):
            raise ValidationError(
                "r2_access_key_id and r2_secret_access_key are required"
                + " when destination includes r2 ❌",
                "Pass them from secrets; R2 has no OIDC federation.",
            )

    flags = {
        "capture_console": cfg.capture_console,
        "dry_run": cfg.dry_run,
        "r2_index": cfg.r2_index,
    }
    for name, value in flags.items():
        if value not in ("true", "false"):
            raise ValidationError(f"{name} must be 'true' or 'false', got '{value}' ❌")

    if cfg.s3_path:
        # Index markers go to the first three path segments; a shallower
        # path would place them outside the intended hierarchy.
        segments = len(trim_slashes(cfg.s3_path).split("/"))
        if dest.s3 and segments < 3:
            raise ValidationError(
                "s3_path must be at least three segments deep ❌",
                f"Received: '{cfg.s3_path}' ({segments} segment(s))",
                "The first three segments receive the index marker.",
            )
    elif not cfg.path_prefix or not cfg.namespace:
        raise ValidationError(
            "path_prefix and namespace cannot be empty ❌",
            "Supply s3_path directly to use a custom layout.",
        )

    if cfg.capture_console == "true" and not cfg.github_token and not dry_run:
        raise ValidationError(
            "capture_console requires github_token ❌",
            "Grant the job actions:read and pass a token.",
        )

    if pip_available is None:
        pip_available = _pip_available()
    if dest.remote and not dry_run and not pip_available:
        raise ValidationError(
            "pip is not available to install boto3 ❌",
            "GitHub hosted runners ship it; self hosted runners may not.",
        )


def run(cfg: Config) -> int:
    """Validate and report in the action log. Returns the exit status."""
    info("Validating inputs...")
    try:
        validate(cfg)
    except ValidationError as err:
        info(f"Error: {err.message}")
        for hint in err.hints:
            info(f"  {hint}")
        return 1
    info("All inputs validated ✅")
    return 0
