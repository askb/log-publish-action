# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Workflow command logging.

Messages often carry file names from the build tree. A newline in one
could start a line the runner reads as a workflow command, so line
breaks are escaped the way the runner expects in command data.
"""

from __future__ import annotations

import sys

_ZWSP = "\u200b"


def _neutralize(message: str) -> str:
    """Break the markers that make the runner treat a line as a command:
    "::" (anywhere it could start one) and the legacy "##["."""
    return message.replace("::", f":{_ZWSP}:").replace("##[", f"#{_ZWSP}#[")


def _escape(message: str) -> str:
    return message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _emit(line: str) -> None:
    # The runner reads workflow commands from stdout, so this is the
    # action's real output channel rather than debug printing.
    _ = sys.stdout.write(line + "\n")
    _ = sys.stdout.flush()


def info(message: str) -> None:
    _emit(_neutralize(message.replace("\r", "\\r").replace("\n", "\\n")))


def warn(message: str) -> None:
    _emit(f"::warning::{_escape(message)}")


def error(message: str) -> None:
    _emit(f"::error::{_escape(message)}")
