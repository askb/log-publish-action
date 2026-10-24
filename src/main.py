# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Entry point: main.py validate | publish."""

from __future__ import annotations

import sys

from log_publish import publish, validate
from log_publish.config import Config


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in ("validate", "publish"):
        _ = sys.stderr.write("usage: main.py validate|publish\n")
        return 2
    cfg = Config.from_env()
    if argv[1] == "validate":
        return validate.run(cfg)
    return publish.run(cfg)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
