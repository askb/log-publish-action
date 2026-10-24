# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Read the action inputs from the INPUT_* environment variables."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

VALID_DESTINATIONS = ("s3", "r2", "artifact", "both")


def _get(env: Mapping[str, str], name: str, default: str = "") -> str:
    """Return an input, treating an empty value as unset like ${X:-d}."""
    value = env.get(name, "")
    return value if value else default


def split_destinations(value: str) -> list[str]:
    """Split a comma or whitespace separated destination list."""
    return [item for item in re.split(r"[,\s]+", value) if item]


@dataclass(frozen=True)
class Destinations:
    """Which stores are active, and the order that picks the primary."""

    s3: bool = False
    r2: bool = False
    artifact: bool = False
    remote_order: tuple[str, ...] = ()

    @property
    def remote(self) -> bool:
        return self.s3 or self.r2


def parse_destinations(value: str) -> Destinations:
    """Interpret a destination list. Unknown members are ignored here and
    rejected by validation, which runs first."""
    s3 = r2 = artifact = False
    order: list[str] = []
    for dest in split_destinations(value):
        if dest in ("s3", "both"):
            s3 = True
            order.append("s3")
            artifact = artifact or dest == "both"
        elif dest == "r2":
            r2 = True
            order.append("r2")
        elif dest == "artifact":
            artifact = True
    return Destinations(s3, r2, artifact, tuple(order))


@dataclass(frozen=True)
class Config:
    """The action inputs plus the runner context they depend on."""

    destination: str = "s3"
    s3_bucket: str = ""
    s3_path: str = ""
    path_prefix: str = "logs"
    namespace: str = "gha"
    job_name: str = "job"
    workspace: str = "."
    archive_dir: str = "archives"
    archive_patterns: str = ""
    capture_console: str = "false"
    include_sysinfo: str = "true"
    compress: str = "true"
    compress_suffix: str = "false"
    index_markers: str = "true"
    base_url: str = ""
    aws_region: str = "us-east-1"
    r2_bucket: str = ""
    r2_account_id: str = ""
    r2_endpoint_url: str = ""
    r2_access_key_id: str = field(default="", repr=False)
    r2_secret_access_key: str = field(default="", repr=False)
    r2_base_url: str = ""
    r2_index: str = "true"
    artifact_name: str = "build-logs"
    github_token: str = field(default="", repr=False)
    dry_run: str = "false"
    permit_fail: str = "true"
    env: Mapping[str, str] = field(default_factory=dict[str, str], repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = dict(os.environ if env is None else env)
        account = _get(env, "INPUT_R2_ACCOUNT_ID")
        endpoint = _get(env, "INPUT_R2_ENDPOINT_URL")
        return cls(
            destination=_get(env, "INPUT_DESTINATION", "s3"),
            s3_bucket=_get(env, "INPUT_S3_BUCKET"),
            s3_path=_get(env, "INPUT_S3_PATH"),
            path_prefix=_get(env, "INPUT_PATH_PREFIX", "logs"),
            namespace=_get(env, "INPUT_NAMESPACE", "gha"),
            job_name=_get(env, "INPUT_JOB_NAME", _get(env, "GITHUB_JOB", "job")),
            workspace=_get(
                env, "INPUT_WORKSPACE", _get(env, "GITHUB_WORKSPACE", os.getcwd())
            ),
            archive_dir=_get(env, "INPUT_ARCHIVE_DIR", "archives"),
            archive_patterns=_get(env, "INPUT_ARCHIVE_PATTERNS"),
            capture_console=_get(env, "INPUT_CAPTURE_CONSOLE", "false"),
            include_sysinfo=_get(env, "INPUT_INCLUDE_SYSINFO", "true"),
            compress=_get(env, "INPUT_COMPRESS", "true"),
            compress_suffix=_get(env, "INPUT_COMPRESS_SUFFIX", "false"),
            index_markers=_get(env, "INPUT_INDEX_MARKERS", "true"),
            base_url=_get(env, "INPUT_BASE_URL"),
            aws_region=_get(env, "INPUT_AWS_REGION", "us-east-1"),
            r2_bucket=_get(env, "INPUT_R2_BUCKET"),
            r2_account_id=account,
            r2_endpoint_url=endpoint,
            r2_access_key_id=_get(env, "INPUT_R2_ACCESS_KEY_ID"),
            r2_secret_access_key=_get(env, "INPUT_R2_SECRET_ACCESS_KEY"),
            r2_base_url=_get(env, "INPUT_R2_BASE_URL"),
            r2_index=_get(env, "INPUT_R2_INDEX", "true"),
            artifact_name=_get(env, "INPUT_ARTIFACT_NAME", "build-logs"),
            github_token=_get(env, "INPUT_GITHUB_TOKEN"),
            dry_run=_get(env, "INPUT_DRY_RUN", "false"),
            permit_fail=_get(env, "INPUT_PERMIT_FAIL", "true"),
            env=env,
        )

    @property
    def destinations(self) -> Destinations:
        return parse_destinations(self.destination)

    @property
    def is_dry_run(self) -> bool:
        return self.dry_run == "true"

    @property
    def r2_endpoint(self) -> str:
        if self.r2_endpoint_url:
            return self.r2_endpoint_url
        if self.r2_account_id:
            return f"https://{self.r2_account_id}.r2.cloudflarestorage.com"
        return ""
