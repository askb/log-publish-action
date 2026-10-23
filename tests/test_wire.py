# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Real boto3 clients, with every request intercepted before it is sent.

Proves, offline, what goes on the wire: which endpoint each store is
called on, under which credentials, and with which headers. In
particular, the R2 token and the AWS OIDC session never mix, and R2
receives no optional checksums it would reject.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import subprocess
import sys
import urllib.parse
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

import pytest

from log_publish import upload
from log_publish.config import Config
from log_publish.upload import ClientFactory, ObjectStore
from tests.conftest import DERIVED, R2_ACCOUNT, R2_ENV, Runner


class _Request(Protocol):
    method: str
    url: str
    headers: Mapping[str, str | bytes]


class _Events(Protocol):
    def register(self, event_name: str, handler: Callable[..., object]) -> None: ...


class _Meta(Protocol):
    events: _Events


class _Client(Protocol):
    meta: _Meta


class _AWSRequestModule(Protocol):
    AWSResponse: Callable[..., object]


def _hooks(client: object) -> _Client:
    return cast(_Client, client)


def _awsrequest(module: object) -> _AWSRequestModule:
    return cast(_AWSRequestModule, module)


AWSResponse = _awsrequest(importlib.import_module("botocore.awsrequest")).AWSResponse


@dataclass
class Sent:
    store: str
    method: str
    url: str
    headers: dict[str, str]

    @property
    def host(self) -> str:
        return urllib.parse.urlsplit(self.url).netloc

    @property
    def key(self) -> str:
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.url).path)
        return path.split(f"/{DERIVED}/", 1)[-1] if DERIVED in path else path


class _Raw:
    def stream(self, **_kwargs: object) -> Iterator[bytes]:
        yield b""


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> list[Sent]:
    """Credentials as configure-aws-credentials leaves them, and no
    config file that could change endpoints or credentials."""
    for name in ("AWS_PROFILE", "AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_S3"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", "/nonexistent")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/nonexistent")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAOIDCSESSION")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "oidc-secret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "oidc-session")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    return []


def factory(sent: list[Sent]) -> ClientFactory:
    def make(name: str, cfg: Config) -> ObjectStore:
        client = upload.boto3_client(name, cfg)

        def before_send(request: _Request, **_kwargs: object) -> object:
            headers = {
                k.lower(): v.decode() if isinstance(v, bytes) else v
                for k, v in request.headers.items()
            }
            sent.append(Sent(name, request.method, request.url, headers))
            return AWSResponse(request.url, 200, {"ETag": '"0"'}, _Raw())

        _hooks(client).meta.events.register("before-send.s3", before_send)
        return client

    return make


def test_dual_publish_on_the_wire(
    run: Runner, workspace: Path, wire: list[Sent]
) -> None:
    result = run(
        workspace,
        factory(wire),
        INPUT_DRY_RUN="false",
        INPUT_DESTINATION="r2,s3",
        **R2_ENV,
    )
    assert result.status == 0, result.stdout
    assert result.outputs["failed_count"] == "0"

    r2 = [s for s in wire if s.store == "r2"]
    s3 = [s for s in wire if s.store == "s3"]
    assert r2 and s3

    for sent in r2:
        assert sent.host.endswith(f"{R2_ACCOUNT}.r2.cloudflarestorage.com")
        auth = sent.headers["authorization"]
        assert "Credential=r2-key-id/" in auth
        assert "/auto/s3/aws4_request" in auth
        assert "x-amz-security-token" not in sent.headers
        assert "oidc" not in str(sent.headers)
        # R2 rejects the optional flexible checksums.
        assert not any(h.startswith("x-amz-checksum-") for h in sent.headers)
        assert "x-amz-sdk-checksum-algorithm" not in sent.headers

    for sent in s3:
        assert sent.host.endswith("amazonaws.com")
        assert "Credential=AKIAOIDCSESSION/" in sent.headers["authorization"]
        assert "/us-east-1/s3/aws4_request" in sent.headers["authorization"]
        assert sent.headers["x-amz-security-token"] == "oidc-session"
        assert "r2-key-id" not in str(sent.headers)

    karaf = next(s for s in r2 if s.key == "karaf.log")
    assert karaf.method == "PUT"
    assert karaf.headers["content-encoding"] == "gzip"
    assert karaf.headers["content-type"] == "text/plain"
    png = next(s for s in r2 if s.key == "screenshot.png")
    assert "content-encoding" not in png.headers
    assert png.headers["content-type"] == "image/png"
    assert any(s.key == "index.html" for s in r2)

    s3_karaf = next(s for s in s3 if s.key == "karaf.log")
    assert "gzip" in s3_karaf.headers["content-encoding"]

    deletes = [s for s in s3 if s.method == "DELETE"]
    assert [urllib.parse.urlsplit(d.url).path.rsplit("/", 2)[-2] for d in deletes] == [
        "logs",
        "gha",
        "yangtools",
    ]


def test_r2_explicit_endpoint(run: Runner, workspace: Path, wire: list[Sent]) -> None:
    result = run(
        workspace,
        factory(wire),
        INPUT_DRY_RUN="false",
        INPUT_DESTINATION="r2",
        INPUT_R2_INDEX="false",
        **{**R2_ENV, "INPUT_R2_ENDPOINT_URL": "https://eu.r2.example"},
    )
    assert result.status == 0
    assert wire and all(s.host.endswith("eu.r2.example") for s in wire)


def test_r2_ignores_an_unknown_aws_profile(
    run: Runner, workspace: Path, wire: list[Sent], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AWS_PROFILE belongs to the S3 side; R2 carries explicit keys."""
    monkeypatch.setenv("AWS_PROFILE", "not-on-this-runner")
    result = run(
        workspace,
        factory(wire),
        INPUT_DRY_RUN="false",
        INPUT_DESTINATION="r2",
        **R2_ENV,
    )
    assert result.status == 0, result.stdout
    assert result.outputs["failed_count"] == "0"
    assert wire and all(s.store == "r2" for s in wire)
    assert all("Credential=r2-key-id/" in s.headers["authorization"] for s in wire)


def test_install_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The runtime install refuses anything not hash pinned in the lock."""
    calls: list[list[str]] = []

    class Done:
        returncode: int = 0
        stderr: str = ""

    def fake_run(cmd: list[str], **_kwargs: object) -> Done:
        calls.append(cmd)
        return Done()

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(upload, "_installed", {})
    # A directory planted by an earlier step is never trusted.
    planted = tmp_path / "log-publish-deps-planted" / "boto3"
    planted.mkdir(parents=True)
    upload.install_boto3()
    upload.install_boto3()
    assert len(calls) == 1, "the second destination reuses this install"
    cmd = calls[0]
    assert cmd[1:4] == ["-I", "-m", "pip"]
    for flag in ("--require-hashes", "--no-deps", "--only-binary=:all:"):
        assert flag in cmd
    target = Path(cmd[cmd.index("--target") + 1])
    assert target.parent == tmp_path
    assert target.name.startswith("log-publish-deps-")
    assert target != planted.parent
    assert sys.path[0] == str(target)
    assert str(planted.parent) not in sys.path
    upload.remove_boto3()
    assert not target.exists()
    assert str(target) not in sys.path
    assert planted.exists(), "only this process's own install is removed"


def test_failed_install_leaves_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Failed:
        returncode: int = 1
        stderr: str = "no matching distribution"

    def fake_run(_cmd: list[str], **_kwargs: object) -> Failed:
        return Failed()

    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("RUNNER_TEMP", str(runner_temp))
    monkeypatch.setattr(upload, "_installed", {})
    with pytest.raises(RuntimeError, match="no matching distribution"):
        upload.install_boto3()
    assert list(runner_temp.iterdir()) == []


def _pins() -> tuple[list[str], list[str]]:
    lines = upload.REQUIREMENTS.read_text().splitlines()
    pins = [line for line in lines if line and not line.startswith((" ", "#"))]
    return lines, pins


def test_requirements_are_pinned_and_hashed() -> None:
    lines, pins = _pins()
    names = {pin.split("==")[0] for pin in pins}
    assert {"boto3", "botocore", "s3transfer"} <= names
    for pin in pins:
        assert "==" in pin, pin
        follow = lines[lines.index(pin) + 1]
        assert follow.strip().startswith("--hash=sha256:"), pin


def test_environment_matches_requirements() -> None:
    """The tests exercise exactly the versions the action installs."""
    _, pins = _pins()
    for pin in pins:
        name, version = pin.split()[0].split("==")
        assert importlib.metadata.version(name) == version, (
            f"{name} differs from requirements.txt;"
            + " run: uv pip install -r requirements.txt"
        )
