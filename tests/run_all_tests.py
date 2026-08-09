"""
Master test runner for Blender 3MF addon.

Runs both unit tests and integration tests in separate Blender processes
using robust CLI flags for deterministic, CI-friendly execution.

Run with:
    python tests/run_all_tests.py

Or against a specific Blender install (e.g. to validate a new version):
    python tests/run_all_tests.py --blender "C:\\Program Files\\Blender Foundation\\Blender 5.2\\blender.exe"

The Blender executable can also be set via the ``BLENDER_EXE`` environment
variable. Explicit ``--blender`` takes precedence over the environment
variable, which takes precedence over the ``blender`` found on PATH.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).parent
PROJECT_ROOT = TESTS_DIR.parent


def build_blender_flags(blender_exe):
    """Build the deterministic Blender CLI flag list for the given executable.

    Deterministic Blender CLI flags:
      --background           No GUI
      --factory-startup      Ignore user prefs / startup file
      --python-exit-code 1   Propagate Python exception → non-zero exit
      -noaudio               Skip audio init (faster)
      -q / --quiet           Suppress Blender status spam
    """
    return [
        blender_exe,
        "--background",
        "--factory-startup",
        "--python-exit-code", "1",
        "-noaudio",
        "-q",
    ]


def run_test_suite(blender_flags, script_name, suite_name):
    """Run a test suite via the configured Blender executable and return success status."""
    script_path = TESTS_DIR / script_name

    print(f"\n{'=' * 70}")
    print(f"RUNNING {suite_name.upper()}")
    print(f"{'=' * 70}\n")

    cmd = blender_flags + ["--python", str(script_path)]
    result = subprocess.run(cmd, cwd=str(PROJECT_ROOT))

    return result.returncode == 0


def main():
    """Run all test suites."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--blender",
        default=os.environ.get("BLENDER_EXE", "blender"),
        help="Path to the Blender executable to test against (default: 'blender' on PATH, "
             "or the BLENDER_EXE environment variable if set).",
    )
    args = parser.parse_args()

    blender_flags = build_blender_flags(args.blender)

    print("=" * 70)
    print("BLENDER 3MF ADDON - FULL TEST SUITE")
    print(f"Blender executable: {args.blender}")
    print("=" * 70)

    results = {}

    # Unit tests (individual functions, real Blender Python — no mocks)
    results["Unit Tests"] = run_test_suite(blender_flags, "run_unit_tests.py", "Unit Tests")

    # Integration tests (operator workflows, round-trips)
    results["Integration Tests"] = run_test_suite(blender_flags, "run_tests.py", "Integration Tests")

    # Final summary
    print("\n" + "=" * 70)
    print("FINAL SUMMARY")
    print("=" * 70)

    all_passed = True
    for name, passed in results.items():
        status = "PASSED" if passed else "FAILED"
        print(f"  {name}: {status}")
        if not passed:
            all_passed = False

    print("-" * 70)
    if all_passed:
        print("ALL TEST SUITES PASSED")
    else:
        print("SOME TEST SUITES FAILED")
    print("=" * 70)

    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
