# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Content types, compression and the staged bytes."""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from log_publish.manifest import content_type_for
from tests.conftest import Runner

ALLURE = "allure-report/**"


@pytest.mark.parametrize(
    ("rel", "ctype"),
    [
        ("allure-report/index.html", "text/html"),
        # lftools would say text/plain for these three.
        ("allure-report/styles.css", "text/css"),
        ("allure-report/app.js", "text/javascript"),
        ("screenshot.png", "image/png"),
        ("allure-report/data/suites.json", "application/json"),
        ("karaf.log", "text/plain"),
    ],
)
def test_content_types(run: Runner, workspace: Path, rel: str, ctype: str) -> None:
    assert run(workspace, INPUT_ARCHIVE_PATTERNS=ALLURE).manifest()[rel][2] == ctype


def test_pattern_scope_and_xml(run: Runner, workspace: Path) -> None:
    rows = run(workspace, INPUT_ARCHIVE_PATTERNS=ALLURE).manifest()
    assert "target/surefire-reports/TEST-a.xml" not in rows
    rows = run(workspace, INPUT_ARCHIVE_PATTERNS="target/**/*.xml").manifest()
    assert rows["target/surefire-reports/TEST-a.xml"][2] == "application/xml"


@pytest.mark.parametrize(
    ("name", "ctype"),
    [
        ("A.HTML", "text/html"),
        ("x.tar.gz", "application/gzip"),
        ("Makefile", "text/plain"),
        ("dir.d/README", "text/plain"),
        ("font.woff2", "font/woff2"),
    ],
)
def test_content_type_table(name: str, ctype: str) -> None:
    assert content_type_for(name) == ctype


def test_compression(run: Runner, workspace: Path) -> None:
    rows = run(workspace, INPUT_ARCHIVE_PATTERNS=ALLURE).manifest()
    assert rows["karaf.log"][3] == "gzip"
    assert rows["screenshot.png"][3] == "-"
    # Compressing must not rename the object, or every relative link
    # inside a report breaks.
    assert rows["allure-report/index.html"][1:4] == [
        "allure-report/index.html",
        "text/html",
        "gzip",
    ]


def test_compress_suffix(run: Runner, workspace: Path) -> None:
    rows = run(
        workspace, INPUT_ARCHIVE_PATTERNS=ALLURE, INPUT_COMPRESS_SUFFIX="true"
    ).manifest()
    assert rows["karaf.log"][1] == "karaf.log.gz"
    # The documented hazard: the linked asset is renamed too.
    assert (
        rows["allure-report/data/suites.json"][1] == "allure-report/data/suites.json.gz"
    )


def test_compress_off(run: Runner, workspace: Path) -> None:
    assert run(workspace, INPUT_COMPRESS="false").manifest()["karaf.log"][3] == "-"


def test_staged_bytes(run: Runner, workspace: Path) -> None:
    result = run(workspace, INPUT_ARCHIVE_PATTERNS=ALLURE)
    staged = result.staging
    assert staged.is_dir()
    assert gzip.decompress((staged / "karaf.log").read_bytes()) == b"karaf started\n"
    css = gzip.decompress((staged / "allure-report/styles.css").read_bytes())
    assert css == b"body{color:red}\n"
    # A binary file is byte identical, not silently gzipped.
    assert (staged / "screenshot.png").read_bytes() == (
        workspace / "archives/screenshot.png"
    ).read_bytes()
    # The link target keeps the name its html points to.
    assert (staged / "allure-report/data/suites.json").is_file()
    # Sizes describe the bytes uploaded.
    size = int(result.manifest()["karaf.log"][4])
    assert size == (staged / "karaf.log").stat().st_size


def test_gzip_is_reproducible(run: Runner, workspace: Path) -> None:
    first = (run(workspace).staging / "karaf.log").read_bytes()
    second = (run(workspace).staging / "karaf.log").read_bytes()
    assert first == second


def test_manifest_is_persisted(run: Runner, workspace: Path, tmp_path: Path) -> None:
    result = run(workspace)
    path = Path(result.outputs["manifest_path"])
    assert path == tmp_path / "runner_temp" / "log-publish-manifest.tsv"
    lines = path.read_text().splitlines()
    assert lines == sorted(lines)
    assert any(
        line.startswith("karaf.log\tkaraf.log\ttext/plain\tgzip\t") for line in lines
    )
