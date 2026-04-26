#!/usr/bin/env python3
"""
check_i18n_keys.py — Backend i18n key coverage gate.

Checks:
  1. Both locales ("en-US", "zh-CN") in MESSAGES have identical key sets.
  2. Every key referenced by tr(locale, "<key>") in backend/api/*.py and
     backend/deps*.py exists in both MESSAGES["en-US"] and MESSAGES["zh-CN"].

Exit 0 on success, exit 1 on any failure.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Locate source root so the script works from any cwd
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent          # backend/scripts/
BACKEND_ROOT = SCRIPT_DIR.parent                       # backend/
PACKAGE_DIR = BACKEND_ROOT / "backend"                 # backend/backend/

# Add the backend package root to sys.path so `from backend.i18n.messages import MESSAGES`
# works without a full `pip install -e .` in every CI environment.
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


# ---------------------------------------------------------------------------
# 1. Import MESSAGES from the live module
# ---------------------------------------------------------------------------
try:
    from backend.i18n.messages import MESSAGES  # type: ignore[import]
except ImportError as exc:
    print(f"[ERROR] Cannot import MESSAGES: {exc}")
    print("        Run from the repo root or ensure the backend package is installed.")
    sys.exit(1)


# ---------------------------------------------------------------------------
# 2. Locale key parity check
# ---------------------------------------------------------------------------
def check_locale_parity() -> list[str]:
    errors: list[str] = []
    en_keys = set(MESSAGES.get("en-US", {}).keys())
    zh_keys = set(MESSAGES.get("zh-CN", {}).keys())

    only_en = sorted(en_keys - zh_keys)
    only_zh = sorted(zh_keys - en_keys)

    if only_en:
        errors.append("Keys present in en-US but MISSING in zh-CN:")
        for k in only_en:
            errors.append(f"  en-US only: {k!r}")

    if only_zh:
        errors.append("Keys present in zh-CN but MISSING in en-US:")
        for k in only_zh:
            errors.append(f"  zh-CN only: {k!r}")

    return errors


# ---------------------------------------------------------------------------
# 3. tr() call-site coverage check
# ---------------------------------------------------------------------------
# Matches: tr(locale, "some.key") or tr(locale, 'some.key')
# Captures just the key string.
_TR_PATTERN = re.compile(r'\btr\s*\(\s*\w+\s*,\s*["\']([^"\']+)["\']')

SOURCE_GLOBS = [
    PACKAGE_DIR / "api" / "*.py",
    PACKAGE_DIR / "deps*.py",
]


def collect_source_files() -> list[Path]:
    files: list[Path] = []
    for pattern in SOURCE_GLOBS:
        files.extend(pattern.parent.glob(pattern.name))
    return sorted(files)


def check_tr_coverage() -> list[str]:
    errors: list[str] = []
    en_keys = set(MESSAGES.get("en-US", {}).keys())
    zh_keys = set(MESSAGES.get("zh-CN", {}).keys())
    all_keys = en_keys | zh_keys

    for src in collect_source_files():
        text = src.read_text(encoding="utf-8")
        for m in _TR_PATTERN.finditer(text):
            key = m.group(1)
            lineno = text[: m.start()].count("\n") + 1
            ref = f"{src.relative_to(BACKEND_ROOT)}:{lineno}"

            if key not in en_keys:
                errors.append(f"  {ref}: key {key!r} missing from MESSAGES['en-US']")
            if key not in zh_keys:
                errors.append(f"  {ref}: key {key!r} missing from MESSAGES['zh-CN']")
            _ = all_keys  # silence unused-variable lint

    return errors


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    all_errors: list[str] = []

    parity_errors = check_locale_parity()
    if parity_errors:
        print("[FAIL] Locale key parity:")
        for line in parity_errors:
            print(f"  {line}")
        all_errors.extend(parity_errors)
    else:
        en_count = len(MESSAGES.get("en-US", {}))
        zh_count = len(MESSAGES.get("zh-CN", {}))
        print(f"[PASS] Locale parity OK — {en_count} keys in en-US, {zh_count} in zh-CN.")

    coverage_errors = check_tr_coverage()
    if coverage_errors:
        print("[FAIL] tr() call-site coverage — referenced keys not in MESSAGES:")
        for line in coverage_errors:
            print(line)
        all_errors.extend(coverage_errors)
    else:
        sources = collect_source_files()
        print(f"[PASS] tr() coverage OK — all referenced keys present ({len(sources)} source files scanned).")

    if all_errors:
        print(f"\n{len(all_errors)} issue(s) found. Fix the above before merging.")
        return 1

    print("\nAll backend i18n checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
