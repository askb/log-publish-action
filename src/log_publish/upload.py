# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Upload the manifest to S3 and Cloudflare R2 through boto3.

boto3 is not imported at module level. The action installs the hash
pinned copy from requirements.txt at run time, and only when something
is really uploaded; a dry run or an artifact only run never needs it.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from .config import Config
from .index import generate_index_pages
from .log import error, info, warn
from .manifest import Entry

ATTEMPTS = 3
MARKER_DEPTH = 3
MARKER_NAME = "_tmpfile"
REQUIREMENTS = Path(__file__).resolve().parents[2] / "requirements.txt"


class ObjectStore(Protocol):
    """The slice of a boto3 S3 client the action uses, with boto3's
    keyword names."""

    def upload_file(
        self,
        Filename: str,
        Bucket: str,
        Key: str,
        ExtraArgs: Mapping[str, str] | None = None,
    ) -> None: ...

    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> object: ...

    def delete_object(self, *, Bucket: str, Key: str) -> object: ...


ClientFactory = Callable[[str, Config], ObjectStore]


class _Session(Protocol):
    def client(self, service_name: str, **kwargs: object) -> ObjectStore: ...


class _SessionModule(Protocol):
    Session: Callable[[], _Session]


class _ConfigModule(Protocol):
    Config: Callable[..., object]


# Installs completed by this process. A directory left by anything else,
# such as an earlier build step, is never trusted.
_installed: dict[Path, Path] = {}


def install_boto3(requirements: Path = REQUIREMENTS) -> None:
    """Install the pinned boto3 into the runner temp area and import it.

    Hash checking mode refuses anything that differs from the lock. The
    target is a fresh, exclusively created directory; the second
    destination reuses the install from this process.
    """
    target = _installed.get(requirements)
    if target is None:
        base = Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir())
        target = Path(tempfile.mkdtemp(prefix="log-publish-deps-", dir=base))
        info("Installing boto3 (hash pinned) ...")
        cmd = [
            sys.executable,
            # Isolated: a pip module in the working directory, user site
            # packages or PYTHON* variables cannot replace the real pip.
            "-I",
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--quiet",
            "--require-hashes",
            "--no-deps",
            "--only-binary=:all:",
            "--target",
            str(target),
            "-r",
            str(requirements),
        ]
        result = subprocess.run(cmd, check=False, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"pip install failed:\n{result.stderr.strip()}")
        _installed[requirements] = target
    if str(target) not in sys.path:
        sys.path.insert(0, str(target))


def _module(name: str) -> object:
    return importlib.import_module(name)


def boto3_client(target: str, cfg: Config) -> ObjectStore:
    """A real client for "s3" or "r2"."""
    sessions = cast(_SessionModule, _module("boto3.session"))
    botocore_config = cast(_ConfigModule, _module("botocore.config"))
    session = sessions.Session()
    if target == "s3":
        # The default credential chain: normally the short lived OIDC
        # session from configure-aws-credentials.
        client = session.client(
            "s3",
            region_name=cfg.aws_region,
            config=botocore_config.Config(retries={"mode": "standard"}),
        )
    else:
        # Explicit keys keep the R2 token and the AWS session apart, which
        # is what makes dual publishing possible. R2 ignores the region
        # but SigV4 needs one, and it rejects the optional checksums newer
        # botocore sends by default.
        client = session.client(
            "s3",
            endpoint_url=cfg.r2_endpoint,
            region_name="auto",
            aws_access_key_id=cfg.r2_access_key_id,
            aws_secret_access_key=cfg.r2_secret_access_key,
            config=botocore_config.Config(
                retries={"mode": "standard"},
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )
    return client


def default_factory(target: str, cfg: Config) -> ObjectStore:
    install_boto3()
    return boto3_client(target, cfg)


@dataclass
class Result:
    uploaded: int = 0
    failed: int = 0


class Publisher:
    """Uploads to one destination with per file retries."""

    def __init__(
        self,
        name: str,
        bucket: str,
        s3_path: str,
        client: ObjectStore,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.name: str = name
        self.bucket: str = bucket
        self.s3_path: str = s3_path
        self.client: ObjectStore = client
        self.sleep: Callable[[float], None] = sleep

    def upload(self, src: Path, key: str, content_type: str, encoding: str) -> bool:
        extra = {"ContentType": content_type}
        if encoding:
            extra["ContentEncoding"] = encoding
        full_key = f"{self.s3_path}/{key}"
        for attempt in range(1, ATTEMPTS + 1):
            try:
                self.client.upload_file(
                    str(src), self.bucket, full_key, ExtraArgs=extra
                )
            except Exception as err:  # any failure is retried
                warn(
                    f"{self.name}: upload attempt {attempt} failed for {key}: "
                    + f"{type(err).__name__}: {err}"
                )
                if attempt < ATTEMPTS:
                    self.sleep((attempt + 1) * 2)
                continue
            return True
        return False

    def upload_all(
        self, staging: Path, entries: list[Entry], pages: list[tuple[Path, str]]
    ) -> Result:
        result = Result()
        jobs = [(staging / e.rel, e.key, e.content_type, e.encoding) for e in entries]
        jobs += [(page, key, "text/html", "") for page, key in pages]
        for src, key, ctype, encoding in jobs:
            if self.upload(src, key, ctype, encoding):
                result.uploaded += 1
            else:
                result.failed += 1
                error(f"{self.name}: failed to upload {key}")
        info(
            f"{self.name}: uploaded {result.uploaded} file(s), "
            + f"{result.failed} failure(s)"
        )
        return result

    def refresh_index_markers(self) -> None:
        """S3 has no directories, so the log server regenerates index.html
        from object events. Writing and removing a marker in each of the
        first three prefixes triggers that regeneration."""
        prefix = ""
        for segment in self.s3_path.split("/")[:MARKER_DEPTH]:
            prefix = f"{prefix}{segment}/"
            key = f"{prefix}{MARKER_NAME}"
            try:
                _ = self.client.put_object(Bucket=self.bucket, Key=key, Body=b"")
            except Exception:  # a marker is best effort
                warn(f"Could not refresh index for {prefix}")
                continue
            try:
                _ = self.client.delete_object(Bucket=self.bucket, Key=key)
            except Exception:
                warn(f"Could not remove index marker {key}")
            info(f"Refreshed index for {prefix}")


@dataclass(frozen=True)
class Staged:
    """The collected tree, ready to upload under s3_path."""

    s3_path: str
    staging: Path
    entries: list[Entry]
    index_dir: Path


def publish_target(
    name: str,
    cfg: Config,
    staged: Staged,
    factory: ClientFactory,
    sleep: Callable[[float], None] = time.sleep,
) -> Result:
    """Upload everything to one destination, counting a client that cannot
    be created as a failure of every file rather than a crash."""
    s3_path, entries = staged.s3_path, staged.entries
    bucket = cfg.s3_bucket if name == "s3" else cfg.r2_bucket
    pages: list[tuple[Path, str]] = []
    if name == "r2" and cfg.r2_index == "true":
        pages = generate_index_pages(
            [e.key for e in entries], s3_path, staged.index_dir
        )
    try:
        client = factory(name, cfg)
    except Exception as err:  # permit_fail decides
        error(f"{name}: cannot create a client: {type(err).__name__}: {err}")
        return Result(failed=len(entries) + len(pages))
    publisher = Publisher(name, bucket, s3_path, client, sleep)
    result = publisher.upload_all(staged.staging, entries, pages)
    if name == "s3" and cfg.index_markers == "true" and result.failed == 0:
        publisher.refresh_index_markers()
    return result
