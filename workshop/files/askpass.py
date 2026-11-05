#!/usr/bin/python3
"""Read only the systemd-provided credential; never persist or log it."""

import os
import re
import stat
import sys
from pathlib import Path


def main() -> int:
    prompt = sys.argv[1] if len(sys.argv) == 2 else ""
    if not re.fullmatch(r"(?:Username|Password) for 'https://(?:[^/@']+@)?github\.com': ?", prompt):
        return 1
    if prompt.startswith("Username"):
        print("x-access-token")
        return 0
    try:
        directory = os.environ["CREDENTIALS_DIRECTORY"]
        if not directory:
            return 1
        path = Path(directory) / "github-token"
        if stat.S_IMODE(path.stat().st_mode) & 0o007:
            return 1
        token = path.read_text().strip()
        if not token or any(char.isspace() for char in token):
            return 1
        print(token)
        return 0
    except (OSError, KeyError):
        return 1


if __name__ == "__main__":
    sys.exit(main())
