# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Outputs, destination routing, R2 index pages and uploads."""

from __future__ import annotations

from pathlib import Path
from typing import NoReturn

from log_publish.config import Config
from log_publish.publish import write_outputs
from tests.conftest import DERIVED, R2_ENV, Run, Runner, Stores

S3_URL = f"https://logs.example.org/{DERIVED}/"
R2_URL = f"https://logs-r2.example.org/{DERIVED}/"


class TestOutputs:
    def test_dry_run(self, run: Runner, workspace: Path) -> None:
        result = run(workspace)
        assert result.status == 0
        assert result.outputs["s3_path"] == DERIVED
        # Exact fields: a substring check on log_url= matches s3_log_url=.
        assert result.outputs["log_url"] == S3_URL
        assert result.outputs["s3_log_url"] == S3_URL
        assert result.outputs["r2_log_url"] == ""
        assert int(result.outputs["file_count"]) >= 5
        assert result.outputs["upload_count"] == "0"
        assert result.outputs["artifact_name"] == ""
        assert result.outputs["staging_dir"] == str(result.staging)
        assert "### 🪵 Build logs published" in result.summary
        assert f"- S3: [Browse logs]({S3_URL})" in result.summary
        assert "- Dry run: no files uploaded" in result.summary

    def test_no_base_url(self, run: Runner, workspace: Path) -> None:
        result = run(workspace, INPUT_BASE_URL="")
        assert result.outputs["log_url"] == ""
        assert f"- S3: `s3://logs-example-org/{DERIVED}/`" in result.summary

    def test_newline_cannot_inject(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        write_outputs(str(out), {"s3_path": "a\nlog_url=https://evil", "x": "1"})
        lines = out.read_text().splitlines()
        assert "log_url=https://evil" in lines
        assert lines[0].startswith("s3_path<<EOF_")
        assert lines[1:3] == ["a", "log_url=https://evil"]
        assert lines[3] == lines[0].split("<<", 1)[1]
        assert lines[4] == "x=1"


class TestArtifact:
    def test_real_run_is_offline(self, run: Runner, workspace: Path) -> None:
        result = run(workspace, INPUT_DESTINATION="artifact", INPUT_DRY_RUN="false")
        assert result.status == 0
        assert result.outputs["failed_count"] == "0"
        assert result.outputs["artifact_name"] == "build-logs"
        assert result.outputs["log_url"] == ""
        # The composite upload step owns this tree from here.
        assert (result.staging / "karaf.log").is_file()
        assert "Workflow artifact: `build-logs`" in result.summary

    def test_custom_name(self, run: Runner, workspace: Path) -> None:
        result = run(
            workspace,
            INPUT_DESTINATION="artifact",
            INPUT_DRY_RUN="false",
            INPUT_ARTIFACT_NAME="logs-jdk21",
        )
        assert result.outputs["artifact_name"] == "logs-jdk21"

    def test_s3_keeps_nothing(
        self, run: Runner, workspace: Path, stores: Stores, tmp_path: Path
    ) -> None:
        result = run(workspace, stores.factory, INPUT_DRY_RUN="false")
        assert result.outputs["artifact_name"] == ""
        assert result.outputs["staging_dir"] == ""
        assert list((tmp_path / "tmp").iterdir()) == []


class TestR2Index:
    def test_pages(self, run: Runner, workspace: Path) -> None:
        _ = (workspace / "archives" / "javascript:alert(1).log").write_text("x")
        result = run(
            workspace,
            INPUT_DESTINATION="r2",
            INPUT_ARCHIVE_PATTERNS="allure-report/**",
            **R2_ENV,
        )
        assert f"r2://odl-logs/{DERIVED}/" in result.stdout
        pages = result.pages()
        assert "index.html" in pages
        assert "allure-report/data/index.html" in pages
        # A report's own index.html is never overwritten.
        assert "allure-report/index.html" not in pages

        root = result.page("index.html")
        assert '<a href="./karaf.log">karaf.log</a>' in root
        assert '<a href="./allure-report/">allure-report/</a>' in root
        assert 'href=""' not in root
        assert 'href="./javascript:alert(1).log"' in root
        assert 'href="javascript:' not in root
        assert 'href="../"' not in root
        assert f"<title>Index of /{DERIVED}/</title>" in root

        data = result.page("allure-report/data/index.html")
        assert '<a href="./suites.json">suites.json</a>' in data
        assert 'href="../"' in data

    def test_directories_sort_first(self, run: Runner, workspace: Path) -> None:
        result = run(
            workspace,
            INPUT_DESTINATION="r2",
            INPUT_ARCHIVE_PATTERNS="allure-report/**",
            **R2_ENV,
        )
        root = result.page("index.html")
        # "_" sorts before "a" by name, but directories come first.
        assert root.index('"./allure-report/"') < root.index('"./_build-details.log"')

    def test_disabled(self, run: Runner, workspace: Path) -> None:
        result = run(
            workspace, INPUT_DESTINATION="r2", INPUT_R2_INDEX="false", **R2_ENV
        )
        assert result.pages() == []

    def test_hostile_names(self, run: Runner, workspace: Path) -> None:
        archives = workspace / "archives"
        evil = archives / "report\nrow\t.."
        evil.mkdir()
        _ = (evil / "result.log").write_text("x")
        _ = (archives / "suite\\result.log").write_text("x")
        (archives / "index.html").mkdir()
        _ = (archives / "index.html" / "result.log").write_text("x")
        (archives / "report\\new").mkdir()
        _ = (archives / "report\\new" / "result.log").write_text("x")
        _ = (archives / 'q"<b>&.log').write_text("x")

        result = run(workspace, INPUT_DESTINATION="r2", **R2_ENV)
        assert "Skipping file with control characters" in result.stdout
        # The name reaches the log escaped, never as a raw line break.
        assert "report\nrow" not in result.stdout
        assert not any("row" in rel for rel in result.manifest())
        assert all(".." not in key for key in result.pages())

        root = result.page("index.html")
        assert 'href="./index.html/"' in root
        assert 'href="./suite%5Cresult.log"' in root
        assert 'href="./q%22%3Cb%3E&amp;.log">q&quot;&lt;b&gt;&amp;.log<' in root
        assert 'href="./result.log"' in result.page("index.html/index.html")
        assert 'href="./result.log"' in result.page("report\\new/index.html")


class TestUploads:
    def dual(
        self, run: Runner, workspace: Path, stores: Stores, order: str, **env: str
    ) -> Run:
        return run(
            workspace,
            stores.factory,
            INPUT_DRY_RUN="false",
            INPUT_DESTINATION=order,
            INPUT_R2_BASE_URL="https://logs-r2.example.org",
            **R2_ENV,
            **env,
        )

    def test_dual_publish(self, run: Runner, workspace: Path, stores: Stores) -> None:
        # A collected _tmpfile shares its name with the S3 index marker;
        # the marker refresh, which runs before R2, must not touch it.
        _ = (workspace / "archives" / "_tmpfile").write_text("user data")
        result = self.dual(run, workspace, stores, "r2,s3")
        assert result.status == 0

        s3_keys = stores.keys("s3")
        r2_keys = stores.keys("r2")
        assert f"{DERIVED}/karaf.log" in s3_keys
        assert f"{DERIVED}/karaf.log" in r2_keys
        assert f"{DERIVED}/_tmpfile" in r2_keys
        assert {c[2] for c in stores.calls if c[0] == "s3"} == {"logs-example-org"}
        assert {c[2] for c in stores.calls if c[0] == "r2"} == {"odl-logs"}

        # Generated pages go to R2 alone.
        assert f"{DERIVED}/index.html" in r2_keys
        assert f"{DERIVED}/index.html" not in s3_keys
        assert stores.stores["r2"].extras[f"{DERIVED}/index.html"] == {
            "ContentType": "text/html"
        }
        assert stores.stores["s3"].extras[f"{DERIVED}/karaf.log"] == {
            "ContentType": "text/plain",
            "ContentEncoding": "gzip",
        }
        assert stores.stores["s3"].extras[f"{DERIVED}/screenshot.png"] == {
            "ContentType": "image/png"
        }

        # Markers: cumulative prefixes, S3 only, each put then deleted.
        markers = ["logs/_tmpfile", "logs/gha/_tmpfile", "logs/gha/yangtools/_tmpfile"]
        assert stores.keys("s3", "put") == markers
        assert stores.keys("s3", "delete") == markers
        assert stores.keys("r2", "put") == []

        assert result.outputs["log_url"] == R2_URL
        assert result.outputs["s3_log_url"] == S3_URL
        assert result.outputs["r2_log_url"] == R2_URL
        assert result.outputs["failed_count"] == "0"
        assert int(result.outputs["upload_count"]) == len(s3_keys) + len(r2_keys)
        assert f"- R2: [Browse logs]({R2_URL})" in result.summary
        assert f"- S3: [Browse logs]({S3_URL})" in result.summary
        assert result.outputs["staging_dir"] == ""

    def test_order_picks_primary(
        self, run: Runner, workspace: Path, stores: Stores
    ) -> None:
        result = self.dual(run, workspace, stores, "s3,r2")
        assert result.outputs["log_url"] == S3_URL

    def test_primary_skips_missing_url(
        self, run: Runner, workspace: Path, stores: Stores
    ) -> None:
        result = self.dual(run, workspace, stores, "s3,r2", INPUT_BASE_URL="")
        assert result.outputs["log_url"] == R2_URL

    def test_markers_off(self, run: Runner, workspace: Path, stores: Stores) -> None:
        _ = self.dual(run, workspace, stores, "s3", INPUT_INDEX_MARKERS="false")
        assert stores.keys("s3", "put") == []

    def test_retry_then_success(
        self, run: Runner, workspace: Path, stores: Stores
    ) -> None:
        attempts: list[str] = []

        def flaky(key: str) -> bool:
            attempts.append(key)
            return key.endswith("karaf.log") and attempts.count(key) < 3

        store = stores.factory("s3", Config())
        store.fail = flaky
        result = self.dual(run, workspace, stores, "s3")
        assert result.outputs["failed_count"] == "0"
        assert "upload attempt 2 failed for karaf.log" in result.stdout
        assert run.sleeps == [4, 6]

    def test_failure_permit_fail(
        self, run: Runner, workspace: Path, stores: Stores
    ) -> None:
        store = stores.factory("s3", Config())
        store.fail = lambda key: key.endswith("karaf.log")
        result = self.dual(run, workspace, stores, "s3")
        assert result.status == 0
        assert result.outputs["failed_count"] == "1"
        assert "::error::s3: failed to upload karaf.log" in result.stdout
        assert "(permit_fail)" in result.stdout
        # A failed upload leaves the server listing alone.
        assert stores.keys("s3", "put") == []
        # Two sleeps per failing file: none after the last attempt.
        assert run.sleeps == [4, 6]
        assert "- S3 uploaded: " in result.summary
        assert ", failed: 1" in result.summary

    def test_failure_strict(self, run: Runner, workspace: Path, stores: Stores) -> None:
        store = stores.factory("s3", Config())
        store.fail = lambda key: key.endswith("karaf.log")
        result = self.dual(run, workspace, stores, "s3", INPUT_PERMIT_FAIL="false")
        assert result.status == 1

    def test_client_error_counts_as_failure(self, run: Runner, workspace: Path) -> None:
        def broken(_name: str, _cfg: Config) -> NoReturn:
            raise RuntimeError("pip install failed")

        result = run(
            workspace, broken, INPUT_DRY_RUN="false", INPUT_PERMIT_FAIL="false"
        )
        assert result.status == 1
        assert "s3: cannot create a client: RuntimeError" in result.stdout
        assert result.outputs["failed_count"] == result.outputs["file_count"]

    def test_r2_only_never_marks(
        self, run: Runner, workspace: Path, stores: Stores
    ) -> None:
        _ = self.dual(run, workspace, stores, "r2")
        assert stores.keys("s3") == []
        assert stores.keys("r2", "put") == []
