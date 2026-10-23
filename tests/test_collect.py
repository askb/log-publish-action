# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Path derivation, collection and glob semantics."""

from __future__ import annotations

import gzip
import io
import os
from http.client import HTTPMessage
from pathlib import Path

import pytest

from log_publish import collect
from log_publish.collect import glob_files
from log_publish.config import Config
from log_publish.paths import slugify
from tests.conftest import DERIVED, Runner


def test_derived_path(run: Runner, workspace: Path) -> None:
    result = run(workspace)
    assert f"s3://logs-example-org/{DERIVED}/" in result.stdout
    assert result.outputs["s3_path"] == DERIVED


def test_matrix_job_name_is_one_segment(run: Runner, workspace: Path) -> None:
    result = run(workspace, INPUT_JOB_NAME="15.1.x / publish-nexus")
    assert result.outputs["s3_path"].endswith("4242/2/15.1.x-publish-nexus")
    other = run(workspace, INPUT_JOB_NAME="16.0.x / publish")
    assert other.outputs["s3_path"] != result.outputs["s3_path"]


def test_s3_path_override(run: Runner, workspace: Path) -> None:
    result = run(workspace, INPUT_S3_PATH="/custom/one/two/three/")
    assert result.outputs["s3_path"] == "custom/one/two/three"


@pytest.mark.parametrize(
    ("raw", "slug"),
    [
        ("15.1.x / publish-nexus", "15.1.x-publish-nexus"),
        ("--Gerrit Maven (JDK 21)--", "gerrit-maven-jdk-21"),
        ("Ünïcode", "n-code"),
        ("x" * 150, "x" * 100),
    ],
)
def test_slugify(raw: str, slug: str) -> None:
    assert slugify(raw) == slug


def test_collection(run: Runner, workspace: Path) -> None:
    rows = run(workspace).manifest()
    assert {"karaf.log", "_build-details.log", "_sys-info.log"} <= rows.keys()
    assert (
        "_sys-info.log" not in run(workspace, INPUT_INCLUDE_SYSINFO="false").manifest()
    )


def test_build_details(run: Runner, workspace: Path) -> None:
    result = run(workspace)
    text = gzip.decompress(
        (result.staging / "_build-details.log").read_bytes()
    ).decode()
    assert (
        "build-url: https://github.com/opendaylight/yangtools/actions/runs/38258444807"
        in text
    )
    assert "job: maven-merge\n" in text


def test_patterns(run: Runner, workspace: Path) -> None:
    patterns = "target/SFT/*.log\n  target/surefire-reports/*.xml  \n\n"
    rows = run(workspace, INPUT_ARCHIVE_PATTERNS=patterns).manifest()
    assert "target/SFT/karaf.log" in rows
    assert "target/surefire-reports/TEST-a.xml" in rows


@pytest.mark.parametrize("pattern", ["/etc/passwd", "../../../etc/passwd"])
def test_unsafe_pattern(run: Runner, workspace: Path, pattern: str) -> None:
    result = run(workspace, INPUT_ARCHIVE_PATTERNS=pattern)
    assert "Ignoring unsafe pattern" in result.stdout
    assert "etc/passwd" not in result.manifest()


def test_symlink_out_of_workspace(run: Runner, workspace: Path, tmp_path: Path) -> None:
    secret = tmp_path / "host-secret.txt"
    _ = secret.write_text("secret")
    (workspace / "target" / "leak.log").symlink_to(secret)
    result = run(workspace, INPUT_ARCHIVE_PATTERNS="target/*.log")
    assert "Skipping file outside the workspace: target/leak.log" in result.stdout
    assert "target/leak.log" not in result.manifest()


def test_archives_skip_symlinks(run: Runner, workspace: Path, tmp_path: Path) -> None:
    secret = tmp_path / "host-secret.txt"
    _ = secret.write_text("secret")
    (workspace / "archives" / "leak.log").symlink_to(secret)
    assert "leak.log" not in run(workspace).manifest()


def test_spaces(run: Runner, workspace: Path) -> None:
    path = workspace / "archives" / "sub dir" / "odd name.log"
    path.parent.mkdir()
    _ = path.write_text("spaced")
    assert "sub dir/odd name.log" in run(workspace).manifest()


def test_empty_workspace(run: Runner, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    result = run(empty)
    assert result.status == 0
    assert "_build-details.log" in result.manifest()


def test_stage_conflict_is_a_warning(run: Runner, workspace: Path) -> None:
    # archives/target is a file, so target/SFT/karaf.log cannot be staged
    # under it; that must not abort the run.
    _ = (workspace / "archives" / "target").write_text("file, not dir")
    result = run(workspace, INPUT_ARCHIVE_PATTERNS="target/SFT/*.log")
    assert result.status == 0
    assert "Could not stage target/SFT/karaf.log" in result.stdout


class TestGlob:
    @pytest.fixture
    def tree(self, tmp_path: Path) -> Path:
        root = tmp_path / "g"
        for rel in ("a.log", ".hidden.log", "d/b.log", "d/e/c.log", "d/.h/x.log"):
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            _ = path.write_text(rel)
        return root

    def rels(self, root: Path, pattern: str) -> set[str]:
        return {p.relative_to(root).as_posix() for p in glob_files(root, pattern)}

    def test_star_matches_dotfiles(self, tree: Path) -> None:
        assert self.rels(tree, "*.log") == {"a.log", ".hidden.log"}

    def test_globstar_zero_or_more(self, tree: Path) -> None:
        assert self.rels(tree, "d/**/*.log") == {"d/b.log", "d/e/c.log", "d/.h/x.log"}

    def test_trailing_globstar(self, tree: Path) -> None:
        assert self.rels(tree, "d/**") == {"d/b.log", "d/e/c.log", "d/.h/x.log"}

    def test_literal_and_class(self, tree: Path) -> None:
        assert self.rels(tree, "d/[c].log") == set()
        assert self.rels(tree, "d/[!a]?log") == {"d/b.log"}
        assert self.rels(tree, "d/[b].log") == {"d/b.log"}
        assert self.rels(tree, "d/missing.log") == set()

    def test_globstar_does_not_follow_dir_links(self, tree: Path) -> None:
        os.symlink(tree / "d", tree / "loop")
        assert not any(r.startswith("loop/") for r in self.rels(tree, "**/*.log"))


class TestConsole:
    def test_redirect_drops_token_off_host(self) -> None:
        import urllib.request

        handler = collect.StripAuthOnRedirect()
        req = urllib.request.Request(
            "https://api.github.com/x", headers={"Authorization": "Bearer t"}
        )
        off = handler.redirect_request(
            req, io.BytesIO(), 302, "Found", HTTPMessage(), "https://blob.example/log"
        )
        same = handler.redirect_request(
            req, io.BytesIO(), 302, "Found", HTTPMessage(), "https://api.github.com/y"
        )
        assert off is not None and not off.has_header("Authorization")
        assert same is not None and same.has_header("Authorization")

    def test_rejects_plain_http(self) -> None:
        with pytest.raises(ValueError):
            _ = collect.api_get("http://api.example/x", "t")

    def test_capture(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        seen: list[str] = []

        def fake_open(url: str, token: str, _timeout: float = 60.0) -> bytes:
            seen.append(url)
            assert token == "tok"
            if url.endswith("per_page=100"):
                return b'{"jobs": [{"id": 1, "name": "Build / JDK 21"}, {"id": 2, "name": "x"}]}'
            if url.endswith("/jobs/2/logs"):
                raise OSError("gone")
            return b"console text"

        monkeypatch.setattr(collect, "api_get", fake_open)
        cfg = Config.from_env(
            {
                "INPUT_GITHUB_TOKEN": "tok",
                "GITHUB_REPOSITORY": "o/r",
                "GITHUB_RUN_ID": "9",
                "GITHUB_RUN_ATTEMPT": "3",
            }
        )
        collect.capture_console(collect.Stager(tmp_path), cfg)
        assert seen[0] == (
            "https://api.github.com/repos/o/r/actions/runs/9/attempts/3/jobs?per_page=100"
        )
        assert (tmp_path / "console" / "build-jdk-21.log").read_text() == "console text"
        assert not (tmp_path / "console" / "x.log").exists()
        assert "Could not capture console log for x" in capsys.readouterr().out

    def test_list_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        def broken(_url: str, _token: str, _timeout: float = 60.0) -> bytes:
            raise OSError("down")

        monkeypatch.setattr(collect, "api_get", broken)
        collect.capture_console(collect.Stager(tmp_path), Config.from_env({}))
        assert "console logs not captured" in capsys.readouterr().out
