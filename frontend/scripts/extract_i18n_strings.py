#!/usr/bin/env python3
"""
extract_i18n_strings.py
-----------------------
Walk frontend/src/**/*.vue  and  **/*.ts / *.js (excluding node_modules / dist),
extract every localizable UI string (template text, attribute values, and JS string
literals that contain at least one Latin word of 2+ chars), and produce:

  frontend/scripts/i18n_inventory.csv   – full inventory

Usage (from repo root or frontend/):
    python3 scripts/extract_i18n_strings.py

The script resolves all paths relative to its own location so it works from any cwd.
"""

import csv
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent.resolve()
SRC_ROOT   = SCRIPT_DIR.parent / "src"
OUT_CSV    = SCRIPT_DIR / "i18n_inventory.csv"

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------
# We match text that contains CJK OR at least one English word (2+ letters)
# and looks like user-facing copy (not code identifiers).

# Template attribute patterns: label="…", placeholder="…", title="…", etc.
RE_ATTR = re.compile(
    r'\b(label|placeholder|title|tooltip|message|description|'
    r'ok-text|cancel-text|sub-title|tab|addon-after|checked-children|'
    r'un-checked-children|hint|suffix|help|content)'
    r'="([^"]{2,})"',
    re.IGNORECASE,
)

# Dynamic :attr="'literal'" or :attr="`literal`"  (single-quoted string in binding)
RE_DATTR = re.compile(
    r':(?:label|placeholder|title|tooltip|message|description|ok-text|cancel-text)'
    r"""="'([^']{2,})'""",
)

# Template text nodes:  >some text<  (trimmed, skip pure whitespace / numbers / ids)
RE_TEXT = re.compile(r'>\s*([A-Za-z][^\n<>{}"\']{1,120}?)\s*<')

# JS / TS string literals (double or single quoted) assigned to message/error/label vars
# or used as object property string values (e.g. title: 'Foo')
RE_JS_STR_DQ = re.compile(r'''(?:message|errorMsg|label|title|tab|placeholder|detail|msg|text|suffix|hint)\s*[=:]\s*"([A-Za-z][^"]{1,120})"''')
RE_JS_STR_SQ = re.compile(r"""(?:message|errorMsg|label|title|tab|placeholder|detail|msg|text|suffix|hint)\s*[=:]\s*'([A-Za-z][^']{1,120})'""")

# Backtick template literals containing an English phrase (no interpolation)
RE_JS_TMPL   = re.compile(r'`([A-Za-z][^`$\n]{2,80})`')

# CJK block (the original requirement — matches characters already translated)
RE_CJK = re.compile(
    r'[一-鿿㐀-䶿豈-﫿]'
)

# English-word filter: string must contain at least one 2+ letter word
RE_HAS_WORD = re.compile(r'[A-Za-z]{2,}')

# Noise filter: skip if the string looks like a CSS value / identifier / format string
RE_NOISE = re.compile(
    r'^(?:'
    r'[a-z][a-z0-9-]+\s*\d*$'       # pure css class / attribute value
    r'|#[0-9a-fA-F]{3,8}$'           # hex colour
    r'|\d[\d.,: %-]+$'               # numbers
    r'|[a-z]+\.[a-z]+$'              # dotted identifiers
    r'|\$[a-z_]+'                     # template variable reference
    r'|(?:grid-template-columns|flex|padding|margin|font-[a-z]+)'
    r')',
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Key-generation helpers
# ---------------------------------------------------------------------------
def _to_snake(text: str) -> str:
    """Turn a short English phrase into snake_case key fragment."""
    text = text.strip().lower()
    # Keep only letters, digits, spaces
    text = re.sub(r'[^a-z0-9 ]', '', text)
    text = re.sub(r'\s+', '_', text)
    text = text[:40].rstrip('_')
    return text or 'text'


def _file_prefix(rel_path: str) -> str:
    """Derive a dot-path prefix from the file location.

    Examples:
        pages/Login.vue          → page.login
        components/ShareDialog   → component.share_dialog
        store/auth.ts            → store.auth
        api/client.ts            → api.client
    """
    parts = Path(rel_path).parts          # e.g. ('pages', 'Login.vue')
    if len(parts) < 1:
        return 'app'
    # strip extension from filename
    stem = Path(parts[-1]).stem           # 'Login'
    stem_snake = re.sub(r'(?<=[a-z])(?=[A-Z])', '_', stem).lower()  # 'login' / 'share_dialog'
    if len(parts) >= 2:
        folder = parts[-2].rstrip('s')    # 'page' / 'component'
        # normalise folder name
        folder = folder.replace('composable', 'composable').replace('utile', 'util')
        return f"{folder}.{stem_snake}"
    return stem_snake


def _attr_suffix(attr_name: str) -> str:
    """Map attribute name to a short suffix for the key."""
    mapping = {
        'placeholder': 'placeholder',
        'label': 'label',
        'title': 'title',
        'message': 'message',
        'description': 'description',
        'ok-text': 'ok',
        'cancel-text': 'cancel',
        'sub-title': 'subtitle',
        'tooltip': 'tooltip',
        'tab': 'tab',
        'addon-after': 'addon',
        'checked-children': 'on',
        'un-checked-children': 'off',
        'hint': 'hint',
        'suffix': 'suffix',
        'help': 'help',
        'content': 'content',
    }
    return mapping.get(attr_name.lower(), attr_name.lower().replace('-', '_'))


# ---------------------------------------------------------------------------
# Core extraction
# ---------------------------------------------------------------------------

def is_localizable(text: str) -> bool:
    text = text.strip()
    if len(text) < 2:
        return False
    if RE_CJK.search(text):
        return True
    if not RE_HAS_WORD.search(text):
        return False
    if RE_NOISE.match(text):
        return False
    # skip import paths, variable names with dots
    if text.count('/') > 1:
        return False
    # skip things that look like CSS/JSX expressions
    if any(c in text for c in ('(', ')', '{', '}', '=>', '&&', '||')):
        return False
    return True


def extract_from_file(path: Path, rel_path: str):
    """Yield (line_no, context_label, text) tuples."""
    try:
        lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return

    for lineno, line in enumerate(lines, 1):
        stripped = line.strip()

        # Skip comment lines
        if stripped.startswith('//') or stripped.startswith('*') or stripped.startswith('#'):
            continue

        # --- attribute patterns (template) ---
        for m in RE_ATTR.finditer(line):
            attr, val = m.group(1), m.group(2)
            val = val.strip()
            if is_localizable(val):
                yield lineno, f'attr:{attr}', val

        for m in RE_DATTR.finditer(line):
            val = m.group(1).strip()
            if is_localizable(val):
                yield lineno, 'attr:dynamic', val

        # --- template text nodes ---
        for m in RE_TEXT.finditer(line):
            val = m.group(1).strip()
            if is_localizable(val):
                yield lineno, 'text', val

        # --- JS string assignments ---
        for pattern, ctx in [(RE_JS_STR_DQ, 'js:string'), (RE_JS_STR_SQ, 'js:string')]:
            for m in pattern.finditer(line):
                val = m.group(1).strip()
                if is_localizable(val):
                    yield lineno, ctx, val

        # --- backtick literals (no interpolation) ---
        for m in RE_JS_TMPL.finditer(line):
            val = m.group(1).strip()
            if is_localizable(val) and '${' not in val:
                yield lineno, 'js:template', val


# ---------------------------------------------------------------------------
# Key assignment with collision resolution
# ---------------------------------------------------------------------------

def assign_keys(rows):
    """Add `suggested_key` to each row dict, resolving collisions with .1 .2 suffixes."""
    seen = defaultdict(int)   # raw_key → count
    result = []
    for row in rows:
        rel_path = row['file']
        prefix   = _file_prefix(rel_path)
        ctx      = row['context']
        text     = row['chinese_text']

        # Build suffix from context type
        if ctx.startswith('attr:'):
            attr_name = ctx[5:]
            key_suffix = _attr_suffix(attr_name)
        elif ctx == 'text':
            key_suffix = _to_snake(text[:20])
        else:  # js:string / js:template
            key_suffix = _to_snake(text[:20])

        raw_key = f"{prefix}.{key_suffix}"
        seen[raw_key] += 1
        count = seen[raw_key]
        final_key = raw_key if count == 1 else f"{raw_key}.{count - 1}"
        row['suggested_key'] = final_key
        result.append(row)
    return result


# ---------------------------------------------------------------------------
# File walker
# ---------------------------------------------------------------------------

def walk_src(src_root: Path):
    SKIP_DIRS = {'node_modules', 'dist', '__tests__', '.git'}
    for root, dirs, files in os.walk(src_root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fname in files:
            if fname.endswith(('.vue', '.ts', '.js')):
                yield Path(root) / fname


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    rows = []
    seen_texts: set[str] = set()         # deduplicate identical strings

    for fpath in sorted(walk_src(SRC_ROOT)):
        rel = str(fpath.relative_to(SRC_ROOT))
        for lineno, context, text in extract_from_file(fpath, rel):
            dedup_key = f"{rel}|{text}"
            if dedup_key in seen_texts:
                continue
            seen_texts.add(dedup_key)
            rows.append({
                'file': rel,
                'line': lineno,
                'context': context,
                'suggested_key': '',        # filled below
                'chinese_text': text,
            })

    rows = assign_keys(rows)

    # Write CSV
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=['file', 'line', 'context', 'suggested_key', 'chinese_text'])
        writer.writeheader()
        writer.writerows(rows)

    # --- frequency analysis for top-30 ---
    freq: dict[str, int] = defaultdict(int)
    text_to_key: dict[str, str] = {}
    for row in rows:
        t = row['chinese_text']
        freq[t] += 1
        if t not in text_to_key:
            text_to_key[t] = row['suggested_key']

    top30 = sorted(freq.items(), key=lambda x: (-x[1], x[0]))[:30]

    print(f"\n{'='*60}")
    print(f"Total localizable strings found: {len(rows)}")
    print(f"Unique strings: {len(freq)}")
    print(f"Output CSV: {OUT_CSV}")
    print(f"\nTop 30 strings by frequency:")
    print(f"{'#':<4} {'Count':<6} {'Key':<45} {'Text'}")
    print('-'*100)
    for i, (text, count) in enumerate(top30, 1):
        key = text_to_key[text]
        print(f"{i:<4} {count:<6} {key:<45} {text[:50]}")

    print(f"\nTotal rows in CSV: {len(rows)}")

    return top30, text_to_key


if __name__ == '__main__':
    top30, text_to_key = main()
