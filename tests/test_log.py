# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Log output cannot be read by the runner as a workflow command."""

from __future__ import annotations

import pytest

from log_publish.log import error, info, warn


@pytest.mark.parametrize(
    "message",
    [
        "::error::spoof.log",
        "  ::add-mask::x",
        "a\n::warning::b",
        "x ##[error]legacy",
    ],
)
def test_info_is_never_a_command(
    message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    info(message)
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    assert "::" not in out
    assert "##[" not in out


def test_warn_and_error_keep_their_command(capsys: pytest.CaptureFixture[str]) -> None:
    warn("bad\nname")
    error("100%")
    assert capsys.readouterr().out.splitlines() == [
        "::warning::bad%0Aname",
        "::error::100%25",
    ]
