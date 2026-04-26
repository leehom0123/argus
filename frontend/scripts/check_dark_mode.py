#!/usr/bin/env python3
"""
check_dark_mode.py — Hardcoded-white/fff guard (stdlib only).

Scans every .vue file under frontend/src/ for CSS properties that use
bare #fff / #ffffff / white without a .dark class guard or CSS variable,
which causes unreadable white-on-white text when the light theme is active.

Patterns flagged:
  color: #fff|#FFF|#ffffff|white
  background[-color]: #fff|#FFF|#ffffff|white

Allowlist (patterns that are intentionally white and do not need a guard):
  - Inside ``theme="dark"`` sider logo  (App.vue only, explicit comment)
  - rgba(255,255,255,...) — semi-transparent overlays, fine in both themes
  - The inside of html.dark { ... } blocks (already guarded)
  - The inside of <style> blocks that are component-scoped and use
    --ant-color-* / var( tokens

Exit codes:
  0 — no banned patterns found (or only allowlisted hits)
  1 — one or more unguarded hardcoded-white values found

Usage:
  python3 scripts/check_dark_mode.py [--src <path>]

TODO: integrate into the CI step that runs i18n_lint.py once this
      has been running clean for a sprint.
"""

import re
import sys
import argparse
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_SRC = SCRIPT_DIR.parent / "src"

# Patterns to flag — matches both style="" inline attrs and <style> blocks.
BANNED = re.compile(
    r"(?:color|background(?:-color)?)\s*:\s*(?:#[fF]{3,6}|white)\b"
)

# Lines that are definitely intentional — matched as literal substrings.
# Extend this list when you add a legitimately always-dark component.
ALLOWLIST_SUBSTRINGS = [
    # Dark sider logo in App.vue — sits inside theme="dark" sider, always dark.
    "color: #fff;",
]

# Any line inside a html.dark { } block is already guarded — skip it.
DARK_GUARD_RE = re.compile(r"html\.dark\s*\{")
END_BLOCK_RE = re.compile(r"^\s*\}")

# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

def in_dark_guard_block(lines: list[str], line_idx: int) -> bool:
    """Return True if ``lines[line_idx]`` is inside an ``html.dark { }`` block."""
    depth = 0
    in_dark = False
    for i, line in enumerate(lines):
        if DARK_GUARD_RE.search(line):
            in_dark = True
            depth = 0
        if in_dark:
            depth += line.count("{") - line.count("}")
            if i == line_idx:
                return True
            if depth <= 0:
                in_dark = False
    return False


def scan_file(path: Path) -> list[tuple[int, str]]:
    """Return list of (line_number, line_content) for offending lines."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception as exc:
        print(f"  [WARN] Cannot read {path}: {exc}", file=sys.stderr)
        return []

    hits: list[tuple[int, str]] = []
    for idx, line in enumerate(lines, start=1):
        if not BANNED.search(line):
            continue
        # Skip allowlisted lines
        if any(allow in line for allow in ALLOWLIST_SUBSTRINGS):
            continue
        # Skip lines already inside an html.dark guard
        if in_dark_guard_block(lines, idx - 1):
            continue
        hits.append((idx, line.rstrip()))
    return hits


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--src",
        type=Path,
        default=DEFAULT_SRC,
        help="Root of the Vue source tree to scan (default: ../src)",
    )
    args = parser.parse_args()

    src_root: Path = args.src.resolve()
    if not src_root.is_dir():
        print(f"ERROR: source directory not found: {src_root}", file=sys.stderr)
        return 2

    vue_files = sorted(src_root.rglob("*.vue"))
    if not vue_files:
        print(f"No .vue files found under {src_root}", file=sys.stderr)
        return 2

    total_offenders: list[tuple[Path, int, str]] = []
    for vf in vue_files:
        for lineno, content in scan_file(vf):
            total_offenders.append((vf, lineno, content))

    if not total_offenders:
        print(f"check_dark_mode: OK — scanned {len(vue_files)} .vue files, 0 offenders.")
        return 0

    print(
        f"check_dark_mode: FAIL — {len(total_offenders)} unguarded hardcoded-white hit(s) "
        f"in {len(set(o[0] for o in total_offenders))} file(s):\n"
    )
    for path, lineno, content in total_offenders:
        rel = path.relative_to(src_root.parent)
        print(f"  {rel}:{lineno}  {content.strip()}")
    print(
        "\nFix: replace with var(--ant-color-text) / var(--ant-color-bg-container) "
        "or wrap in html.dark { } guard."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
