#!/usr/bin/env python3
"""
i18n_lint.py — Bilingual locale parity checker (stdlib only).

Checks that zh-CN.ts and en-US.ts:
  1. Contain exactly the same set of keys (at every nesting level).
  2. Have no empty-string values in en-US.ts (which signals an untranslated entry).

Exit codes:
  0 — all checks passed
  1 — parity failure or untranslated values found
  2 — locale files not yet generated
"""

import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = SCRIPT_DIR.parent
LOCALE_DIR = FRONTEND_DIR / "src" / "i18n" / "locales"

ZH_FILE = LOCALE_DIR / "zh-CN.ts"
EN_FILE = LOCALE_DIR / "en-US.ts"

# ---------------------------------------------------------------------------
# Parser: shallow-to-deep TS object literal → nested dict of leaf paths
# ---------------------------------------------------------------------------

def _strip_ts_export(source: str) -> str:
    """Remove `export default` / `as const` wrapping and return the raw object text."""
    # Drop single-line // comments
    source = re.sub(r"//[^\n]*", "", source)
    # Drop block comments
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    # Remove `as const` suffix and trailing semicolons
    source = re.sub(r"\bas\s+const\b", "", source)
    source = source.strip().rstrip(";")
    # Drop `const NAME = ` or `export default ` prefix
    source = re.sub(r"^\s*(export\s+default\s+|const\s+\w+\s*=\s*)", "", source, flags=re.DOTALL)
    return source.strip()


def _parse_object(text: str, pos: int) -> tuple[dict, int]:
    """
    Recursively parse a JS/TS object literal starting at `pos` (the `{`).
    Returns (parsed_dict, index_after_closing_brace).

    Values are either nested dicts or string literals (single/double/backtick).
    """
    assert text[pos] == "{", f"Expected '{{' at pos {pos}, got {text[pos]!r}"
    result = {}
    i = pos + 1
    n = len(text)

    while i < n:
        # Skip whitespace and commas
        while i < n and text[i] in " \t\n\r,":
            i += 1

        if i >= n:
            raise SyntaxError("Unterminated object")

        if text[i] == "}":
            return result, i + 1

        # Parse key: bare identifier or quoted string
        if text[i] in ("'", '"', "`"):
            quote = text[i]
            j = i + 1
            while j < n and text[j] != quote:
                if text[j] == "\\" :
                    j += 1  # skip escaped char
                j += 1
            key = text[i + 1:j]
            i = j + 1
        elif re.match(r"[A-Za-z_$]", text[i]):
            j = i
            while j < n and re.match(r"\w", text[j]):
                j += 1
            key = text[i:j]
            i = j
        else:
            raise SyntaxError(f"Unexpected character at pos {i}: {text[i]!r}")

        # Skip whitespace
        while i < n and text[i] in " \t\n\r":
            i += 1

        if i >= n or text[i] != ":":
            raise SyntaxError(f"Expected ':' after key {key!r} at pos {i}")
        i += 1  # consume ':'

        # Skip whitespace
        while i < n and text[i] in " \t\n\r":
            i += 1

        if i >= n:
            raise SyntaxError(f"Missing value for key {key!r}")

        if text[i] == "{":
            value, i = _parse_object(text, i)
        elif text[i] in ("'", '"', "`"):
            quote = text[i]
            j = i + 1
            while j < n and text[j] != quote:
                if text[j] == "\\":
                    j += 1
                j += 1
            value = text[i + 1:j]
            i = j + 1
        else:
            # Non-string primitive (number, boolean, etc.) — read until comma/}
            j = i
            while j < n and text[j] not in (",", "}", "\n"):
                j += 1
            value = text[i:j].strip()
            i = j

        result[key] = value

    raise SyntaxError("Unterminated object: missing closing '}'")


def parse_locale_file(path: Path) -> dict:
    """Parse a TS locale file and return the nested dict."""
    source = path.read_text(encoding="utf-8")
    obj_text = _strip_ts_export(source)

    # Find the first `{`
    start = obj_text.find("{")
    if start == -1:
        raise ValueError(f"No object literal found in {path}")

    parsed, _ = _parse_object(obj_text, start)
    return parsed


# ---------------------------------------------------------------------------
# Key extraction (flat dotted paths)
# ---------------------------------------------------------------------------

def flatten_keys(obj: dict, prefix: str = "") -> set[str]:
    """Recursively collect all dotted key paths."""
    keys = set()
    for k, v in obj.items():
        full = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            keys |= flatten_keys(v, full)
        else:
            keys.add(full)
    return keys


def collect_empty_values(obj: dict, prefix: str = "") -> list[str]:
    """Return dotted paths whose value is the empty string."""
    empties = []
    for k, v in obj.items():
        full = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            empties.extend(collect_empty_values(v, full))
        elif isinstance(v, str) and v == "":
            empties.append(full)
    return empties


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    missing = [p for p in (ZH_FILE, EN_FILE) if not p.exists()]
    if missing:
        for p in missing:
            print(f"  missing: {p}")
        print("\nlocale files not yet generated; run Dev-1 / Dev-2 tasks first")
        return 2

    # Parse both files
    try:
        zh = parse_locale_file(ZH_FILE)
    except Exception as exc:
        print(f"[ERROR] Failed to parse {ZH_FILE}: {exc}")
        return 1

    try:
        en = parse_locale_file(EN_FILE)
    except Exception as exc:
        print(f"[ERROR] Failed to parse {EN_FILE}: {exc}")
        return 1

    zh_keys = flatten_keys(zh)
    en_keys = flatten_keys(en)

    errors: list[str] = []

    # --- Check 1: key parity ---
    only_in_zh = sorted(zh_keys - en_keys)
    only_in_en = sorted(en_keys - zh_keys)

    if only_in_zh or only_in_en:
        errors.append("Key mismatch between zh-CN and en-US:")
        for k in only_in_zh:
            errors.append(f"  + zh-CN only:  {k}")
        for k in only_in_en:
            errors.append(f"  + en-US only:  {k}")

    # --- Check 2: untranslated en-US values ---
    empties = collect_empty_values(en)
    if empties:
        errors.append("Untranslated (empty string) values in en-US:")
        for k in sorted(empties):
            errors.append(f"  - {k}")

    if errors:
        print("[FAIL] i18n parity check failed:")
        for line in errors:
            print(line)
        return 1

    zh_count = len(zh_keys)
    en_count = len(en_keys)
    print(f"[PASS] i18n parity OK — {zh_count} keys in zh-CN, {en_count} keys in en-US, all matched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
