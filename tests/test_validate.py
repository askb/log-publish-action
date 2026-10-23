# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Input validation, run as its own step before any work."""

from __future__ import annotations

import pytest

from log_publish import validate
from log_publish.config import Config
from tests.conftest import R2_ACCOUNT


def check(pip: bool = True, /, **env: str) -> str:
    """Return the validation error, or "" when the inputs pass."""
    cfg = Config.from_env({f"INPUT_{k.upper()}": v for k, v in env.items()})
    try:
        validate.validate(cfg, pip_available=pip)
    except validate.ValidationError as err:
        return err.message
    return ""


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"s3_bucket": ""}, "s3_bucket is required"),
        ({"s3_bucket": "Not_A_Bucket"}, "not a valid bucket name"),
        (
            {"s3_bucket": "logs-example-org", "s3_path": "logs/gha"},
            "at least three segments",
        ),
        (
            {
                "s3_bucket": "logs-example-org",
                "capture_console": "true",
                "dry_run": "false",
            },
            "requires github_token",
        ),
        (
            {"s3_bucket": "logs-example-org", "dry_run": "maybe"},
            "must be 'true' or 'false'",
        ),
        (
            {"s3_bucket": "logs-example-org", "destination": "ftp"},
            "destination must list s3, r2, artifact or both",
        ),
        (
            {"s3_bucket": "logs-example-org", "destination": "s3,gcs"},
            "destination must list",
        ),
        ({"destination": " , "}, "destination must list"),
        ({"destination": "both"}, "s3_bucket is required"),
        ({"destination": "s3"}, "s3_bucket is required"),
        ({"destination": "r2", "dry_run": "true"}, "r2_bucket is required"),
        (
            {"destination": "r2", "r2_bucket": "Bad.Bucket", "dry_run": "true"},
            "not a valid bucket name",
        ),
        (
            {"destination": "r2", "r2_bucket": "odl-logs", "dry_run": "true"},
            "r2_account_id",
        ),
        (
            {
                "destination": "r2",
                "r2_bucket": "odl-logs",
                "r2_endpoint_url": "http://insecure.example",
                "dry_run": "true",
            },
            "must be an https URL",
        ),
        (
            {
                "destination": "r2",
                "r2_bucket": "odl-logs",
                "r2_account_id": R2_ACCOUNT,
                "dry_run": "false",
            },
            "r2_secret_access_key are required",
        ),
    ],
)
def test_rejects(env: dict[str, str], message: str) -> None:
    assert message in check(**env)


@pytest.mark.parametrize(
    "env",
    [
        {
            "s3_bucket": "logs-example-org",
            "dry_run": "true",
            "s3_path": "logs/gha/yangtools/verify/1",
        },
        # The artifact destination needs no bucket and no credentials.
        {"destination": "artifact", "dry_run": "false"},
        # The depth rule only places S3 markers.
        {"destination": "artifact", "s3_path": "shallow", "dry_run": "false"},
        {
            "s3_bucket": "logs-example-org",
            "s3_path": "ci/run1",
            "index_markers": "false",
            "dry_run": "true",
        },
        {
            "destination": "r2",
            "r2_bucket": "odl-logs",
            "r2_account_id": R2_ACCOUNT,
            "dry_run": "true",
        },
        {
            "destination": "s3, r2,artifact",
            "s3_bucket": "logs-example-org",
            "r2_bucket": "odl-logs",
            "r2_account_id": R2_ACCOUNT,
            "dry_run": "true",
        },
        {
            "destination": "r2",
            "r2_bucket": "odl-logs",
            "r2_endpoint_url": "https://eu.r2.example",
            "r2_access_key_id": "k",
            "r2_secret_access_key": "s",
        },
    ],
)
def test_accepts(env: dict[str, str]) -> None:
    assert check(**env) == ""


@pytest.mark.parametrize(
    "flag",
    [
        "capture_console",
        "compress",
        "compress_suffix",
        "dry_run",
        "include_sysinfo",
        "index_markers",
        "permit_fail",
        "r2_index",
    ],
)
@pytest.mark.parametrize("value", ["yes", "False", "nope", "1"])
def test_rejects_non_boolean_flags(flag: str, value: str) -> None:
    env = {"destination": "artifact", flag: value}
    assert check(**env) == f"{flag} must be 'true' or 'false', got '{value}' ❌"


def test_real_upload_needs_pip() -> None:
    assert "pip is not available" in check(False, s3_bucket="logs-example-org")
    assert check(False, s3_bucket="logs-example-org", dry_run="true") == ""
    assert check(False, destination="artifact") == ""


def test_run_reports(capsys: pytest.CaptureFixture[str]) -> None:
    cfg = Config.from_env({"INPUT_S3_BUCKET": "logs-example-org", "INPUT_S3_PATH": "a"})
    assert validate.run(cfg) == 1
    out = capsys.readouterr().out
    assert "Error: s3_path must be at least three segments deep ❌" in out
    assert "Received: 'a' (1 segment(s))" in out
