# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Shared fixtures: a realistic build tree and an in-process runner.

Everything runs offline. Real uploads go to recording fakes or, in
test_wire.py, to real boto3 clients whose requests are intercepted
before they reach the network.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from log_publish import publish
from log_publish.config import Config
from log_publish.upload import ClientFactory

R2_ACCOUNT = "0123456789abcdef0123456789abcdef"
DERIVED = "logs/gha/yangtools/gerrit-maven-merge/4242/2/maven-merge"

BASE_ENV = {
    "GITHUB_REPOSITORY": "opendaylight/yangtools",
    "GITHUB_WORKFLOW": "Gerrit Maven Merge",
    "GITHUB_RUN_NUMBER": "4242",
    "GITHUB_RUN_ATTEMPT": "2",
    "GITHUB_RUN_ID": "38258444807",
    "GITHUB_SERVER_URL": "https://github.com",
    "GITHUB_SHA": "deadbeef",
    "GITHUB_REF": "refs/heads/master",
    "GITHUB_JOB": "maven-merge",
    "GITHUB_ACTOR": "askb",
    "GITHUB_EVENT_NAME": "push",
    "RUNNER_OS": "Linux",
    "INPUT_S3_BUCKET": "logs-example-org",
    "INPUT_DRY_RUN": "true",
    "INPUT_BASE_URL": "https://logs.example.org",
}

R2_ENV = {
    "INPUT_R2_BUCKET": "odl-logs",
    "INPUT_R2_ACCOUNT_ID": R2_ACCOUNT,
    "INPUT_R2_ACCESS_KEY_ID": "r2-key-id",
    "INPUT_R2_SECRET_ACCESS_KEY": "r2-secret",
}


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A throwaway tree that looks like a real Maven/Allure build."""
    ws = tmp_path / "ws"
    files = {
        "archives/karaf.log": b"karaf started\n",
        "archives/notes.txt": b"some notes\n",
        "archives/screenshot.png": b"PNG\x89fakebinary",
        "target/SFT/karaf.log": b"feature test output\n",
        "target/surefire-reports/TEST-a.xml": b"<testsuite/>\n",
        "allure-report/index.html": b'<html><a href="data/suites.json">x</a></html>\n',
        "allure-report/styles.css": b"body{color:red}\n",
        "allure-report/app.js": b"console.log(1)\n",
        "allure-report/data/suites.json": b'{"uid":"1"}\n',
        "allure-report/history/history.json": b'{"items":[]}\n',
    }
    for rel, data in files.items():
        path = ws / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_bytes(data)
    return ws


@dataclass
class Run:
    """What one publish run printed, wrote and returned."""

    status: int
    stdout: str
    outputs: dict[str, str]
    summary: str

    def manifest(self) -> dict[str, list[str]]:
        """Dry run manifest rows keyed by staged path."""
        rows: dict[str, list[str]] = {}
        in_manifest = False
        for line in self.stdout.splitlines():
            if line == "--- manifest ---":
                in_manifest = True
                continue
            if in_manifest:
                cols = line.split("\t")
                if len(cols) != 5:
                    break
                rows[cols[0]] = cols
        return rows

    def field(self, prefix: str) -> str:
        for line in self.stdout.splitlines():
            if line.startswith(prefix):
                return line[len(prefix) :]
        return ""

    @property
    def staging(self) -> Path:
        return Path(self.outputs.get("staging_dir") or self.field("staging-dir: "))

    @property
    def index_dir(self) -> Path:
        return Path(self.field("index-dir: "))

    def page(self, key: str) -> str:
        """A generated R2 index page by its object key."""
        for line in (self.index_dir / "pages.tsv").read_text().splitlines():
            page, page_key = line.split("\t")
            if page_key == key:
                return Path(page).read_text()
        return ""

    def pages(self) -> list[str]:
        marker = "--- r2 index pages ---"
        lines = self.stdout.splitlines()
        if marker not in lines:
            return []
        rest = lines[lines.index(marker) + 1 :]
        return [line for line in rest if not line.startswith(("Log ", "::"))]


@dataclass
class Runner:
    tmp_path: Path
    capsys: pytest.CaptureFixture[str]
    sleeps: list[float] = field(default_factory=list)

    def __call__(
        self,
        ws: Path,
        factory: ClientFactory | None = None,
        /,
        **env: str,
    ) -> Run:
        out = self.tmp_path / "github_output"
        summary = self.tmp_path / "step_summary"
        runner_temp = self.tmp_path / "runner_temp"
        runner_temp.mkdir(exist_ok=True)
        for path in (out, summary):
            _ = path.write_text("")
        merged = {
            **BASE_ENV,
            "INPUT_WORKSPACE": str(ws),
            "GITHUB_OUTPUT": str(out),
            "GITHUB_STEP_SUMMARY": str(summary),
            "RUNNER_TEMP": str(runner_temp),
            **env,
        }
        _ = self.capsys.readouterr()
        status = publish.run(Config.from_env(merged), factory, self.sleeps.append)
        outputs = dict(
            line.split("=", 1) for line in out.read_text().splitlines() if "=" in line
        )
        return Run(status, self.capsys.readouterr().out, outputs, summary.read_text())


@pytest.fixture(autouse=True)
def private_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep staging trees inside tmp_path so nothing leaks into /tmp."""
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp))
    yield


@pytest.fixture
def run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Runner:
    return Runner(tmp_path, capsys)


@dataclass
class FakeStore:
    """Records calls; like the real client, fails on a missing source."""

    name: str
    calls: list[tuple[str, ...]]
    extras: dict[str, dict[str, str]] = field(default_factory=dict)
    fail: Callable[[str], bool] = lambda _key: False

    def upload_file(
        self,
        Filename: str,
        Bucket: str,
        Key: str,
        ExtraArgs: Mapping[str, str] | None = None,
    ) -> None:
        if not Path(Filename).is_file():
            raise FileNotFoundError(Filename)
        if self.fail(Key):
            raise ConnectionError("simulated failure")
        self.calls.append((self.name, "upload", Bucket, Key))
        self.extras[Key] = dict(ExtraArgs or {})

    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> object:
        assert Body == b""
        self.calls.append((self.name, "put", Bucket, Key))
        return {}

    def delete_object(self, *, Bucket: str, Key: str) -> object:
        self.calls.append((self.name, "delete", Bucket, Key))
        return {}


@dataclass
class Stores:
    calls: list[tuple[str, ...]] = field(default_factory=list)
    stores: dict[str, FakeStore] = field(default_factory=dict)

    def factory(self, name: str, _cfg: Config) -> FakeStore:
        return self.stores.setdefault(name, FakeStore(name, self.calls))

    def keys(self, name: str, op: str = "upload") -> list[str]:
        return [c[3] for c in self.calls if c[0] == name and c[1] == op]


@pytest.fixture
def stores() -> Stores:
    return Stores()
