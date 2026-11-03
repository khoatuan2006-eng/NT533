"""Run the repository's Python quality gates using the active environment."""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-tests", action="store_true", help="Run lint/format gates only")
    args = parser.parse_args()
    commands = [
        [sys.executable, "-m", "ruff", "check", "src", "scripts", "tests"],
        [sys.executable, "-m", "ruff", "format", "--check", "src", "scripts", "tests"],
    ]
    if not args.skip_tests:
        commands.append([sys.executable, "-m", "pytest", "-q"])
    for command in commands:
        result = subprocess.run(command, cwd=ROOT)
        if result.returncode:
            raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
