"""
One-time setup for the Elite Dangerous Expedition Tracker.

  Windows:  double-click install.bat
            or: python install.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent

_SEP  = "=" * 56
_TICK = "  [OK]  "
_WARN = "  [!!]  "
_FAIL = "  [XX]  "


def _header() -> None:
    print()
    print(_SEP)
    print("   Elite Dangerous Expedition Tracker — Setup")
    print(_SEP)
    print()


def _check_python() -> None:
    print("Checking Python version …")
    major, minor = sys.version_info[:2]
    if (major, minor) < (3, 11):
        print(f"{_FAIL}Python 3.11+ is required (you have {major}.{minor}).")
        print()
        print("  Download the latest Python from https://python.org")
        print("  Make sure to tick 'Add Python to PATH' during install.")
        sys.exit(1)
    print(f"{_TICK}Python {major}.{minor}")


def _install_deps() -> None:
    print()
    print("Installing dependencies …")
    req = ROOT / "requirements.txt"
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", str(req), "--quiet"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"{_FAIL}pip install failed:")
        print(result.stderr.strip())
        sys.exit(1)
    print(f"{_TICK}Dependencies installed")


def _create_shortcut() -> None:
    print()
    print("Creating Desktop shortcut …")
    result = subprocess.run(
        [sys.executable, str(ROOT / "make_shortcut.py")],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    if result.returncode != 0:
        print(f"{_WARN}Shortcut creation failed (non-fatal):")
        print(f"       {result.stderr.strip()}")
        print(f"       Run 'python make_shortcut.py' manually to retry.")
    else:
        print(f"{_TICK}Desktop shortcut created (ED Tracker.lnk)")


def _open_config() -> None:
    print()
    print("Opening configuration window …")
    subprocess.Popen(
        [sys.executable, str(ROOT / "ui" / "config_window.py")],
        cwd=str(ROOT),
    )


def _footer() -> None:
    print()
    print(_SEP)
    print("  Setup complete!")
    print(_SEP)
    print()
    print("  The configuration window is now open.")
    print("  Fill in your commander name, journal folder,")
    print("  and expedition route, then click Save.")
    print()
    print("  To start a session:")
    print("    double-click 'ED Tracker' on your Desktop")
    print("    — or —")
    print("    pythonw tracker.pyw")
    print()


def main() -> None:
    _header()
    _check_python()
    _install_deps()
    _create_shortcut()
    _open_config()
    _footer()


if __name__ == "__main__":
    main()
