# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Collect, compress and publish: the action's main step."""

from __future__ import annotations

import secrets
import shutil
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from .collect import collect
from .config import Config
from .index import generate_index_pages
from .log import error, info, warn
from .manifest import Entry, build_manifest
from .paths import resolve_path
from .upload import (
    ClientFactory,
    Result,
    Staged,
    default_factory,
    publish_target,
    remove_boto3,
)


def write_outputs(path: str, outputs: dict[str, str]) -> None:
    """Append to GITHUB_OUTPUT, using the delimiter form for any value with
    a line break so it cannot inject a second output."""
    with open(path, "a", encoding="utf-8") as handle:
        for name, value in outputs.items():
            if "\n" in value or "\r" in value:
                delimiter = f"EOF_{secrets.token_hex(8)}"
                _ = handle.write(f"{name}<<{delimiter}\n{value}\n{delimiter}\n")
            else:
                _ = handle.write(f"{name}={value}\n")


def _url(base: str, s3_path: str) -> str:
    return f"{base.rstrip('/')}/{s3_path}/" if base else ""


def run(
    cfg: Config,
    factory: ClientFactory | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Publish and return the step's exit status."""
    dest = cfg.destinations
    dry_run = cfg.is_dry_run
    s3_path = resolve_path(cfg)

    if dest.s3:
        info(f"Publishing to s3://{cfg.s3_bucket}/{s3_path}/")
    if dest.r2:
        info(f"Publishing to r2://{cfg.r2_bucket}/{s3_path}/")
    if dest.artifact:
        info(f"Publishing as workflow artifact (destination: {cfg.destination})")

    staging = Path(tempfile.mkdtemp(prefix="log-publish."))
    index_dir = Path(f"{staging}.index")
    keep_staging = dry_run or dest.artifact
    try:
        collect(cfg, staging)
        entries = build_manifest(
            staging, cfg.compress == "true", cfg.compress_suffix == "true"
        )
        info(f"Collected {len(entries)} file(s)")
        if not entries:
            warn("No files collected; nothing to publish")

        results: dict[str, Result] = {}
        if dry_run:
            _report_dry_run(cfg, s3_path, staging, entries, index_dir)
        else:
            staged = Staged(s3_path, staging, entries, index_dir)
            for name in ("s3", "r2"):
                if getattr(dest, name):
                    results[name] = publish_target(
                        name, cfg, staged, factory or default_factory, sleep
                    )
    finally:
        # A dry run exists to be inspected, so its tree and index pages
        # stay. The artifact destination uploads the tree from a later
        # composite step, which owns the cleanup instead.
        if not dry_run:
            shutil.rmtree(index_dir, ignore_errors=True)
        if not keep_staging:
            shutil.rmtree(staging, ignore_errors=True)
        remove_boto3()

    staging_out = str(staging) if keep_staging else ""
    artifact_out = cfg.artifact_name if dest.artifact and not dry_run else ""
    if artifact_out:
        info(f"Staged tree retained for artifact upload: {staging}")

    s3_url = _url(cfg.base_url, s3_path) if dest.s3 else ""
    r2_url = _url(cfg.r2_base_url, s3_path) if dest.r2 else ""
    urls = {"s3": s3_url, "r2": r2_url}
    # The first destination listed that has a public URL is the primary.
    log_url = next((urls[d] for d in dest.remote_order if urls[d]), "")

    uploaded = sum(r.uploaded for r in results.values())
    failed = sum(r.failed for r in results.values())

    runner_temp = cfg.env.get("RUNNER_TEMP") or "/tmp"
    manifest_path = Path(runner_temp) / "log-publish-manifest.tsv"
    try:
        _ = manifest_path.write_text(
            "".join(f"{e.row()}\n" for e in entries), encoding="utf-8"
        )
    except OSError:
        warn(f"Could not write {manifest_path}")

    if cfg.env.get("GITHUB_OUTPUT"):
        write_outputs(
            cfg.env["GITHUB_OUTPUT"],
            {
                "s3_path": s3_path,
                "log_url": log_url,
                "s3_log_url": s3_url,
                "r2_log_url": r2_url,
                "file_count": str(len(entries)),
                "upload_count": str(uploaded),
                "failed_count": str(failed),
                "manifest_path": str(manifest_path),
                "staging_dir": staging_out,
                "artifact_name": artifact_out,
            },
        )

    if cfg.env.get("GITHUB_STEP_SUMMARY"):
        summary = _summary(cfg, s3_path, urls, artifact_out, len(entries), results)
        with open(cfg.env["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            _ = handle.write(summary)

    if failed:
        if cfg.permit_fail == "true":
            warn(f"{failed} file(s) failed to upload (permit_fail) ⚠️")
            return 0
        error(f"{failed} file(s) failed to upload ❌")
        return 1
    info("Log publishing complete ✅")
    return 0


def _report_dry_run(
    cfg: Config, s3_path: str, staging: Path, entries: list[Entry], index_dir: Path
) -> None:
    info("Dry run: no S3 or R2 calls will be made")
    info(f"staging-dir: {staging}")
    info("--- manifest ---")
    for entry in entries:
        info(entry.row())
    if cfg.destinations.r2 and cfg.r2_index == "true":
        pages = generate_index_pages([e.key for e in entries], s3_path, index_dir)
        info(f"index-dir: {index_dir}")
        info("--- r2 index pages ---")
        for key in sorted(key for _page, key in pages):
            info(key)


def _summary(
    cfg: Config,
    s3_path: str,
    urls: dict[str, str],
    artifact_name: str,
    file_count: int,
    results: dict[str, Result],
) -> str:
    dest = cfg.destinations
    lines = ["### 🪵 Build logs published", ""]
    for name, label, bucket, scheme in (
        ("s3", "S3", cfg.s3_bucket, "s3"),
        ("r2", "R2", cfg.r2_bucket, "r2"),
    ):
        if not getattr(dest, name):
            continue
        if urls[name]:
            lines.append(f"- {label}: [Browse logs]({urls[name]})")
        else:
            lines.append(f"- {label}: `{scheme}://{bucket}/{s3_path}/`")
    if dest.artifact and not cfg.is_dry_run:
        lines.append(f"Workflow artifact: `{artifact_name}`")
    lines += [
        "",
        f"- Destination: {cfg.destination}",
        f"- Files collected: {file_count}",
    ]
    if cfg.is_dry_run:
        lines.append("- Dry run: no files uploaded")
    else:
        for name, label in (("s3", "S3"), ("r2", "R2")):
            if name in results:
                r = results[name]
                lines.append(f"- {label} uploaded: {r.uploaded}, failed: {r.failed}")
    return "\n".join(lines) + "\n"
