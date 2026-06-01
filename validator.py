#!/usr/bin/env python3
"""
YAML Validator and Corrector
Validates YAML input and attempts to auto-correct common mistakes.
"""

import sys
import os
import re
import argparse
import textwrap
from dataclasses import dataclass, field
from typing import Optional


try:
    import yaml
except ImportError:
    print("Error: PyYAML is not installed. Run: pip install pyyaml", file=sys.stderr)
    sys.exit(1)


# ─────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────

@dataclass
class Issue:
    line: Optional[int]
    column: Optional[int]
    severity: str          # "error" | "warning" | "info"
    code: str
    message: str
    suggestion: str = ""

    def __str__(self) -> str:
        loc = f"line {self.line}" if self.line else "global"
        if self.column:
            loc += f", col {self.column}"
        icon = {"error": "✖", "warning": "⚠", "info": "ℹ"}.get(self.severity, "?")
        parts = [f"  {icon} [{self.code}] {loc}: {self.message}"]
        if self.suggestion:
            parts.append(f"    → {self.suggestion}")
        return "\n".join(parts)


@dataclass
class ValidationResult:
    valid: bool
    issues: list[Issue] = field(default_factory=list)
    corrected_yaml: Optional[str] = None
    corrections_made: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────
# Correctors
# ─────────────────────────────────────────────

def fix_tabs(text: str, result: ValidationResult) -> str:
    """
    Replace tabs used for indentation with 2 spaces.

    Handles tabs anywhere in the *leading* whitespace of a line — not just at
    column 0 — so mixed indentation like ``"  \\tkey:"`` (spaces then a tab) is
    normalized too.  Tabs inside values are left untouched.
    """
    lines = text.splitlines(keepends=True)
    changed = False
    out = []
    for line in lines:
        stripped = line.lstrip(" \t")
        indent = line[: len(line) - len(stripped)]
        if "\t" in indent:
            line = indent.replace("\t", "  ") + stripped
            changed = True
        out.append(line)
    if changed:
        result.corrections_made.append("Replaced tab indentation with 2-space indentation")
    return "".join(out)


def _protected_lines(text: str) -> set[int]:
    """
    Return the set of 0-indexed line numbers that live inside a block scalar
    (`key: |` or `key: >`).  Content inside block scalars is literal text and
    must never be touched by the colon/quoting correctors.
    """
    lines = text.split("\n")
    protected: set[int] = set()
    key_block_re = re.compile(r'^(\s*)(?:[\w\-\.\"\']+|-)\s*:?\s*[|>][+\-]?\d*\s*(?:#.*)?$')
    i, n = 0, len(lines)
    while i < n:
        m = key_block_re.match(lines[i])
        if m:
            indent = len(m.group(1))
            j = i + 1
            while j < n:
                content = lines[j]
                if content.strip() == "":
                    protected.add(j)
                    j += 1
                    continue
                cur_indent = len(content) - len(content.lstrip(" "))
                if cur_indent > indent:
                    protected.add(j)
                    j += 1
                else:
                    break
            i = j
        else:
            i += 1
    return protected


def fix_missing_space_after_colon(text: str, result: ValidationResult) -> str:
    """
    Add a space after the colon in `key:value` pairs where it is missing.

    Operates line by line (skipping block-scalar content) so it can also flag
    each occurrence as a warning — even when the document still parses as a
    valid string (e.g. `foo:bar` parses as the string "foo:bar", not a mapping).
    """
    protected = _protected_lines(text)
    lines = text.split("\n")
    # key at line start, colon, then a non-space/non-slash char (slash avoids URLs)
    pattern = re.compile(r'^(\s*)([\w\-\.]+):([^\s/])')
    count = 0
    for idx, line in enumerate(lines):
        if idx in protected:
            continue
        m = pattern.match(line)
        if not m:
            continue
        lines[idx] = pattern.sub(r'\1\2: \3', line, count=1)
        count += 1
        result.issues.append(Issue(
            line=idx + 1,
            column=len(m.group(1)) + len(m.group(2)) + 1,
            severity="warning",
            code="W005",
            message=f"Missing space after colon in key '{m.group(2)}'",
            suggestion="YAML needs a space after the colon, e.g. 'key: value'",
        ))
    if count:
        result.corrections_made.append(
            f"Added missing space after colon in {count} key-value pair(s)"
        )
    return "\n".join(lines)


def fix_trailing_whitespace(text: str, result: ValidationResult) -> str:
    """Remove trailing spaces/tabs from each line."""
    # splitlines(keepends=False) so we can rstrip the content cleanly,
    # then rejoin.  We must preserve whether the file ends with a newline.
    had_trailing_newline = text.endswith("\n")
    lines = text.splitlines()  # no keepends — newlines are stripped
    out = []
    changed = False
    for line in lines:
        stripped = line.rstrip(" \t")
        if stripped != line:
            changed = True
        out.append(stripped)
    if changed:
        result.corrections_made.append("Removed trailing whitespace")
    joined = "\n".join(out)
    if had_trailing_newline:
        joined += "\n"
    return joined


def fix_windows_newlines(text: str, result: ValidationResult) -> str:
    """Normalise CRLF to LF."""
    if "\r\n" in text:
        result.corrections_made.append("Converted Windows (CRLF) line endings to Unix (LF)")
        return text.replace("\r\n", "\n")
    return text


def fix_duplicate_keys(text: str, result: ValidationResult) -> str:
    """
    Remove subsequent duplicate keys within the same mapping block.
    Uses an indent-stack to correctly scope sibling keys — keys at the same
    indentation level but under *different* parent keys are not flagged.
    """
    lines = text.splitlines(keepends=True)
    key_re = re.compile(r'^(\s*)([\w\-\.\"\']+)\s*:')

    # Each stack entry: (indent_level, seen_keys_dict {key → first_line_no})
    # The bottom entry covers top-level keys.
    indent_stack: list[tuple[int, dict[str, int]]] = [(-1, {})]

    skip_set: set[int] = set()
    removed = 0
    i = 0
    while i < len(lines):
        if i in skip_set:
            i += 1
            continue
        m = key_re.match(lines[i])
        if m:
            indent = len(m.group(1))
            key = m.group(2).strip("\"'")

            # Pop stack frames that are at the same or deeper indent than current line,
            # which means we've left those nested blocks.
            while len(indent_stack) > 1 and indent_stack[-1][0] >= indent:
                indent_stack.pop()

            current_seen = indent_stack[-1][1]

            if key in current_seen:
                # Duplicate in the same mapping block
                result.issues.append(Issue(
                    line=i + 1,
                    column=indent + 1,
                    severity="warning",
                    code="W002",
                    message=f"Duplicate key '{key}' (first seen at line {current_seen[key]})",
                    suggestion="Remove or rename the duplicate key",
                ))
                # Mark the duplicate block (this key + any deeper children) for removal
                j = i + 1
                while j < len(lines):
                    m2 = key_re.match(lines[j])
                    if m2 and len(m2.group(1)) <= indent:
                        break
                    j += 1
                for x in range(i, j):
                    skip_set.add(x)
                removed += 1
                i = j
                continue
            else:
                current_seen[key] = i + 1
                # Push a new scope for children of this key
                indent_stack.append((indent, {}))
        i += 1

    if removed:
        result.corrections_made.append(f"Removed {removed} duplicate key block(s)")
        text = "".join(line for idx, line in enumerate(lines) if idx not in skip_set)
    return text


def fix_unquoted_colon_in_value(text: str, result: ValidationResult) -> str:
    """
    Quote values that contain a bare colon followed by a space, e.g.
      title: Hello: World   →   title: "Hello: World"
    Only applies when the value isn't already quoted or a block scalar.

    Uses [^\n] instead of . to guarantee the pattern never spans lines.
    """
    # Group 1: key + ": " prefix. Group 2: the value. Anchored per line.
    pattern = re.compile(r'^([ \t]*[\w\-\.]+[ \t]*:[ \t]+)(.+)$')

    def fix_value(prefix: str, value: str) -> Optional[str]:
        val = value.strip()
        # skip already-quoted, block scalars, booleans, nulls, numbers, comments
        if (val.startswith(('"', "'", '|', '>', '{', '[', '#'))
                or val in ('true', 'false', 'null', 'True', 'False', 'Null',
                           '~', 'yes', 'no', 'on', 'off',
                           'YES', 'NO', 'ON', 'OFF', 'Yes', 'No', 'On', 'Off')
                or re.match(r'^-?\d', val)):
            return None
        # only quote if there's a colon-space sequence in the value
        if ': ' in val:
            return prefix + '"' + val.replace('\\', '\\\\').replace('"', '\\"') + '"'
        return None

    protected = _protected_lines(text)
    lines = text.split("\n")
    count = 0
    for idx, line in enumerate(lines):
        if idx in protected:
            continue
        m = pattern.match(line)
        if not m:
            continue
        replaced = fix_value(m.group(1), m.group(2))
        if replaced is not None:
            lines[idx] = replaced
            count += 1
    if count:
        result.corrections_made.append(
            f"Quoted {count} value(s) that contained unescaped colons"
        )
    return "\n".join(lines)


def fix_missing_newline_at_eof(text: str, result: ValidationResult) -> str:
    """Ensure file ends with a newline."""
    if text and not text.endswith("\n"):
        result.corrections_made.append("Added missing newline at end of file")
        return text + "\n"
    return text


# ─────────────────────────────────────────────
# Static linting (warnings that don't auto-fix)
# ─────────────────────────────────────────────

def lint(text: str, result: ValidationResult) -> None:
    """Emit non-fatal warnings without modifying text."""
    lines = text.splitlines()

    for i, line in enumerate(lines, 1):
        # Bare 'yes' / 'no' as boolean values (YAML 1.1 footgun)
        if re.search(r':\s+(yes|no|on|off)\s*$', line, re.IGNORECASE):
            val = re.search(r':\s+(\S+)', line).group(1)
            result.issues.append(Issue(
                line=i, column=None,
                severity="warning",
                code="W003",
                message=f"'{val}' is treated as a boolean in YAML 1.1",
                suggestion=f"Quote it as '\"{val}\"' if you mean the string",
            ))

        # Octal-looking integers like 0777
        if re.search(r':\s+0[0-7]+\s*$', line):
            result.issues.append(Issue(
                line=i, column=None,
                severity="warning",
                code="W004",
                message="Value looks like an octal integer (YAML 1.1 parses 0-prefixed numbers as octal)",
                suggestion="Quote the value or use explicit decimal notation",
            ))

        # Lines over 120 chars
        if len(line) > 120:
            result.issues.append(Issue(
                line=i, column=None,
                severity="info",
                code="I001",
                message=f"Long line ({len(line)} chars) — consider wrapping",
            ))


def fix_broken_indentation(text: str, result: ValidationResult) -> str:
    """
    Repair inconsistent / broken indentation.

    YAML indentation is semantic, so this is necessarily heuristic.  We walk the
    document tracking *relative* nesting — a line indented more than the previous
    significant line is a child; an equal indent is a sibling; a smaller indent
    dedents to a matching ancestor — and re-emit every line at a clean,
    consistent 2-spaces-per-level indent.  This repairs the most common breakage:
    odd indent widths (1, 3, 5 spaces), mixed widths, and over/under-indented
    keys or list items.

    The rewrite is applied ONLY if the result parses as valid YAML, so the file
    is never made worse: if the heuristic can't produce something valid, the
    original text is returned untouched and the safety net elsewhere takes over.
    """
    if _parses(text):
        return text  # structure already valid — don't disturb it

    lines = text.split("\n")
    protected = _protected_lines(text)

    out: list[str] = []
    raw_levels: Optional[list[int]] = None  # raw indent width per logical depth
    prev_opens_block = False  # did the previous significant line open a child block?
    changed = False

    for idx, line in enumerate(lines):
        stripped = line.strip()
        # Leave blank lines, comments and block-scalar content exactly as-is.
        if idx in protected or stripped == "" or stripped.startswith("#"):
            out.append(line)
            continue

        raw_indent = len(line) - len(line.lstrip(" "))
        content = line.lstrip(" ")

        if raw_levels is None:
            # Anchor depth 0 to the first significant line's indent, so a
            # uniformly over-indented document is pulled back to column 0.
            raw_levels = [raw_indent]
        elif raw_indent > raw_levels[-1] and prev_opens_block:
            # Deeper than the previous line, AND that line can actually hold
            # children (it ended with ':' or was a bare '-') → real nesting.
            raw_levels.append(raw_indent)
        else:
            # Otherwise realign to the NEAREST existing level instead of
            # inventing a new one.  This repairs a sibling that was accidentally
            # under/over-indented (e.g. 3 spaces where 2 were meant), since a
            # key with an inline value cannot legally have deeper children.
            # Ties break toward the deeper level (closer sibling).
            best_depth, best_dist = 0, abs(raw_levels[0] - raw_indent)
            for d in range(1, len(raw_levels)):
                dist = abs(raw_levels[d] - raw_indent)
                if dist <= best_dist:
                    best_dist, best_depth = dist, d
            del raw_levels[best_depth + 1:]

        depth = len(raw_levels) - 1
        new_line = "  " * depth + content
        if new_line != line:
            changed = True
        out.append(new_line)

        # A line opens a child block if it ends with a colon (`key:`, `- key:`)
        # or is a bare sequence dash (`-`) — i.e. it has no inline scalar value.
        no_comment = re.split(r"\s+#", stripped, maxsplit=1)[0].rstrip()
        prev_opens_block = no_comment.endswith(":") or no_comment == "-"

    if not changed:
        return text

    candidate = "\n".join(out)
    if _parses(candidate):
        result.corrections_made.append("Normalized inconsistent indentation")
        return candidate
    return text  # re-indent didn't yield valid YAML — leave the file untouched


def fix_missing_colon(text: str, result: ValidationResult) -> str:
    """
    Insert a missing key/value colon on lines that look like a mapping entry
    written without one (e.g. ``name John`` → ``name: John``).

    This is heuristic: YAML happily reads ``name John`` as the plain string
    "name John" (and several such lines fold into one scalar), so a missing
    colon cannot be detected by parsing alone.  To stay high-precision we only
    rewrite a colon-less ``key value`` line when an *explicit* ``key: value``
    mapping sibling exists in the **same mapping block** — i.e. a true sibling
    sharing the same document, parent, and indent, not merely the same column
    somewhere else in the file.  Folded plain scalars, scalar values under a
    block key, and lines whose value merely contains a colon are all left
    untouched.  The rewrite is additionally applied only if the result still
    parses, so the file is never made worse.
    """
    protected = _protected_lines(text)
    lines = text.split("\n")

    # A bare "<key> <value>" line: identifier-like key, whitespace, then a value.
    cand_re = re.compile(r'^(\s*)([A-Za-z_][A-Za-z0-9_\-\.]*)(\s+)(\S.*?)\s*$')
    # An explicit mapping key: anchored "<key>:" with a space or end-of-line
    # after the colon (so "note Hello: world" is NOT counted as a mapping).
    mapping_re = re.compile(r'^(\s*)([\w\-\.\"\']+)\s*:(\s|$)')
    # A real YAML document marker only lives at column 0 (so indented "---" or
    # "---" sitting inside a block scalar is NOT a marker).
    marker_re = re.compile(r'^(---|\.\.\.)(\s|$)')

    candidates: dict[int, tuple[int, str, str, int]] = {}  # idx → (indent, key, value, keycol)
    group_of: dict[int, tuple] = {}                         # candidate idx → sibling group
    groups_with_mapping: set[tuple] = set()

    # Walk every structural line, tracking the open-parent stack so each line's
    # sibling group is (document, parent line, indent).  Mapping evidence is
    # then scoped to true siblings rather than to a global column width.
    stack: list[tuple[int, int]] = []  # (indent, line_index) of open parents
    doc_id = 0
    for idx, line in enumerate(lines):
        # Block-scalar content is literal text — it must not affect document
        # scope or the parent stack (e.g. a "---" line inside a "|" block).
        if idx in protected:
            continue
        if marker_re.match(line):
            doc_id += 1   # new document → its own scope
            stack = []
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue  # blank/comment lines have no structural effect
        indent = len(line) - len(line.lstrip())
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1] if stack else None
        stack.append((indent, idx))

        group = (doc_id, parent, indent)
        if mapping_re.match(line):
            groups_with_mapping.add(group)   # a real mapping sibling lives here
            continue
        # Skip sequence items and anything that already contains a colon.
        if stripped.startswith("-") or ":" in line:
            continue
        m = cand_re.match(line)
        if not m:
            continue
        candidates[idx] = (indent, m.group(2), m.group(4), len(m.group(1)) + 1)
        group_of[idx] = group

    if not candidates:
        return text

    # Fix a candidate only when one of its *true siblings* is an explicit
    # mapping key — never on the strength of colon-less lines alone (those are
    # indistinguishable from a valid folded plain scalar).
    to_fix = {idx for idx in candidates if group_of[idx] in groups_with_mapping}
    if not to_fix:
        return text

    new_lines = list(lines)
    issues: list[Issue] = []
    for idx in sorted(to_fix):
        indent, key, value, keycol = candidates[idx]
        new_lines[idx] = f"{' ' * indent}{key}: {value}"
        issues.append(Issue(
            line=idx + 1,
            column=keycol,
            severity="warning",
            code="W006",
            message=f"Missing colon after key '{key}'",
            suggestion="A mapping entry needs a colon, e.g. 'key: value'",
        ))

    candidate = "\n".join(new_lines)
    if not _parses(candidate):
        return text  # couldn't safely turn this into valid YAML — leave it alone

    result.issues.extend(issues)
    result.corrections_made.append(
        f"Inserted missing colon in {len(to_fix)} key-value pair(s)"
    )
    return candidate


# ─────────────────────────────────────────────
# Core validate + correct pipeline
# ─────────────────────────────────────────────

CORRECTORS = [
    fix_windows_newlines,
    fix_tabs,
    fix_trailing_whitespace,
    fix_missing_space_after_colon,
    fix_missing_colon,
    fix_unquoted_colon_in_value,
    fix_broken_indentation,
    fix_duplicate_keys,
    fix_missing_newline_at_eof,
]

MAX_ATTEMPTS = 5


def parse_yaml_error(exc: yaml.YAMLError) -> Issue:
    """Convert a PyYAML exception into an Issue."""
    msg = str(exc)
    line = col = None
    if hasattr(exc, 'problem_mark') and exc.problem_mark:
        line = exc.problem_mark.line + 1
        col = exc.problem_mark.column + 1
    problem = getattr(exc, 'problem', str(exc)) or str(exc)
    context = getattr(exc, 'context', None)
    full_msg = problem
    if context:
        full_msg = f"{context}; {problem}"

    suggestion = ""
    p = problem.lower()
    if "tab" in p:
        suggestion = "Replace tab characters with spaces"
    elif "mapping values" in p:
        suggestion = "Ensure there is a space after the colon in your key: value pairs"
    elif "could not find expected ':'" in p:
        suggestion = "A key is missing its colon"
    elif "duplicate key" in p:
        suggestion = "Remove or rename the duplicate key"
    elif "expected <block end>" in p:
        suggestion = "Check your indentation — a block may not be closed properly"
    elif "special characters" in p:
        suggestion = "Wrap the value in quotes"

    return Issue(
        line=line, column=col,
        severity="error",
        code="E001",
        message=full_msg,
        suggestion=suggestion,
    )


def _parses(text: str) -> bool:
    try:
        list(yaml.safe_load_all(text))
        return True
    except yaml.YAMLError:
        return False


def _cosmetic_only(text: str) -> tuple[str, list[str]]:
    """Run only the safe, non-semantic fixes. Returns (text, corrections)."""
    r = ValidationResult(valid=True)
    working = text
    for fix in (fix_windows_newlines, fix_tabs, fix_trailing_whitespace,
                fix_missing_newline_at_eof):
        working = fix(working, r)
    return working, r.corrections_made


def validate_and_correct(text: str) -> ValidationResult:
    """
    Validate YAML and build a corrected version.

    The full corrector pipeline always runs — even when the input already
    parses — so issues like `foo:bar` (a missing space that YAML silently
    reads as the string "foo:bar") are still detected and fixed.  A safety
    net guarantees we never hand back something worse than the input: if the
    correctors would turn parseable YAML into unparseable YAML, we fall back
    to cosmetic-only fixes.
    """
    result = ValidationResult(valid=False)
    original_parses = _parses(text)

    # Run all correctors, repeating until stable or MAX_ATTEMPTS reached.
    working = text
    all_corrections: list[str] = []
    all_issues: list[Issue] = []

    for _ in range(MAX_ATTEMPTS):
        pass_result = ValidationResult(valid=False)
        for fix in CORRECTORS:
            working = fix(working, pass_result)
        all_corrections.extend(pass_result.corrections_made)
        all_issues.extend(i for i in pass_result.issues if i.severity != "error")
        if not pass_result.corrections_made:
            break  # stable — nothing more to fix
        if _parses(working):
            break  # parses cleanly, no need for more passes

    corrected_parses = _parses(working)

    # Case 1: the corrected text parses cleanly — best outcome.
    if corrected_parses:
        result.valid = True
        result.corrections_made = _dedup_ordered(all_corrections)
        result.issues = all_issues
        lint(working, result)
        if result.corrections_made:
            result.corrected_yaml = working
        return result

    # Case 2: corrected text does NOT parse, but the original DID.
    # A corrector broke valid YAML — fall back to cosmetic-only fixes.
    if original_parses:
        safe, safe_corrections = _cosmetic_only(text)
        result.valid = True
        result.corrections_made = safe_corrections
        lint(safe, result)
        if safe_corrections:
            result.corrected_yaml = safe
        return result

    # Case 3: neither the original nor the corrected version parses.
    # Report the parse error and still return the best-effort corrected text.
    result.valid = False
    result.corrections_made = _dedup_ordered(all_corrections)
    result.issues = all_issues
    try:
        list(yaml.safe_load_all(working))
    except yaml.YAMLError as exc:
        result.issues.insert(0, parse_yaml_error(exc))
    result.corrected_yaml = working
    return result


def _dedup_ordered(items: list[str]) -> list[str]:
    """Return items with duplicates removed, preserving first-occurrence order."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="yaml-validator",
        description="Validate and auto-correct YAML files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            examples:
              yaml-validator config.yaml
              yaml-validator config.yaml --fix          # writes config.fixed.yaml
              yaml-validator config.yaml --output clean.yaml
              cat broken.yaml | yaml-validator -
              yaml-validator *.yaml --summary
        """),
    )
    p.add_argument(
        "files",
        nargs="*",
        metavar="FILE",
        help="YAML file(s) to validate. Use '-' to read from stdin.",
    )
    p.add_argument(
        "--fix", "-f",
        action="store_true",
        help="Write corrected YAML to a SEPARATE file (e.g. config.yaml → "
             "config.fixed.yaml). The original file is never modified.",
    )
    p.add_argument(
        "--output", "-o",
        metavar="FILE",
        help="Write corrected YAML to this specific path instead of the "
             "auto-named .fixed file (only valid with a single input file).",
    )
    p.add_argument(
        "--print-corrected", "-p",
        action="store_true",
        help="Print the corrected YAML to stdout.",
    )
    p.add_argument(
        "--summary", "-s",
        action="store_true",
        help="Print a one-line summary per file instead of full details.",
    )
    p.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI colour output.",
    )
    p.add_argument(
        "--strict",
        action="store_true",
        help="Exit with code 1 even if only warnings were found.",
    )
    return p


# ANSI helpers
def _c(code: str, text: str, no_color: bool) -> str:
    if no_color:
        return text
    return f"\033[{code}m{text}\033[0m"

def green(t, nc=False):  return _c("32", t, nc)
def red(t, nc=False):    return _c("31", t, nc)
def yellow(t, nc=False): return _c("33", t, nc)
def bold(t, nc=False):   return _c("1",  t, nc)
def dim(t, nc=False):    return _c("2",  t, nc)


def report_file(
    label: str,
    text: str,
    result: ValidationResult,
    args: argparse.Namespace,
) -> bool:
    """Print result for one file. Returns True if there are errors."""
    nc = args.no_color
    errors   = [i for i in result.issues if i.severity == "error"]
    warnings = [i for i in result.issues if i.severity == "warning"]
    infos    = [i for i in result.issues if i.severity == "info"]

    status = green("✔ valid", nc) if result.valid else red("✖ invalid", nc)

    if args.summary:
        parts = [f"{bold(label, nc)}: {status}"]
        if errors:   parts.append(red(f"{len(errors)} error(s)", nc))
        if warnings: parts.append(yellow(f"{len(warnings)} warning(s)", nc))
        if result.corrections_made:
            parts.append(dim(f"{len(result.corrections_made)} correction(s)", nc))
        print("  ".join(parts))
        return bool(errors)

    print(f"\n{'─' * 60}")
    print(f"  {bold(label, nc)}  {status}")
    print(f"{'─' * 60}")

    if errors:
        print(red(f"\n  Errors ({len(errors)}):", nc))
        for issue in errors:
            print(red(str(issue), nc))
        # PyYAML aborts at the first syntax error, so additional structural
        # problems may be hidden until this one is resolved.
        print(dim("    (parsing stops at the first error — fix it and re-run "
                   "to reveal any others)", nc))

    if warnings:
        print(yellow(f"\n  Warnings ({len(warnings)}):", nc))
        for issue in warnings:
            print(yellow(str(issue), nc))

    if infos:
        print(dim(f"\n  Info ({len(infos)}):", nc))
        for issue in infos:
            print(dim(str(issue), nc))

    if result.corrections_made:
        # Be honest about whether anything is actually written to disk.
        # A file is only written when --fix or --output is supplied.
        will_write = bool(getattr(args, "fix", False) or getattr(args, "output", None))
        header = "Corrections applied:" if will_write else "Corrections available (not written yet):"
        print(f"\n  {bold(header, nc)}")
        for c in result.corrections_made:
            print(f"    • {c}")
        if not will_write:
            hint = _fixed_path(label) if label != "<stdin>" else "<name>.fixed.yaml"
            print(yellow(f"\n  → Nothing was saved. Re-run with --fix to write the "
                         f"corrected YAML to {hint}", nc))
            print(yellow("    (or use --output <file> to choose the path, "
                         "or --print-corrected to print it).", nc))

    if not result.issues and not result.corrections_made:
        print(f"  {dim('No issues found.', nc)}")

    return bool(errors)


def _fixed_path(path: str) -> str:
    """Derive the separate output path for a fixed file: foo.yaml → foo.fixed.yaml."""
    root, ext = os.path.splitext(path)
    return f"{root}.fixed{ext or '.yaml'}"


def process_file(
    label: str,
    text: str,
    args: argparse.Namespace,
    source_path: Optional[str] = None,
) -> ValidationResult:
    """Validate one file and handle output/fix flags. Returns the result."""
    result = validate_and_correct(text)
    report_file(label, text, result, args)

    corrected = result.corrected_yaml

    if corrected and args.print_corrected:
        print(f"\n{'─' * 60}")
        print(f"  Corrected YAML ({label})")
        print(f"{'─' * 60}\n")
        print(corrected)

    # Decide whether to write a corrected file. We ALWAYS write to a separate
    # file — the original is never modified.
    if args.output or args.fix:
        if not result.corrections_made:
            print("\n  No changes needed — nothing to write.")
        elif corrected is None:
            print("\n  Could not produce a corrected version.")
        else:
            if args.output:
                out_path = args.output
            elif source_path and source_path != "<stdin>":
                out_path = _fixed_path(source_path)
            else:
                out_path = "fixed.yaml"  # stdin fallback

            # Safety: never overwrite the original input file (resolve symlinks too).
            if (source_path and source_path != "<stdin>"
                    and os.path.realpath(out_path) == os.path.realpath(source_path)):
                out_path = _fixed_path(source_path)
                print("\n  (refusing to overwrite the original — using a separate file)")

            with open(out_path, "w", encoding="utf-8") as fh:
                fh.write(corrected)
            print(f"\n  Wrote corrected YAML → {out_path}")
            if not result.valid:
                print("  ⚠ Note: auto-correction could not fully fix this file — "
                      "the written copy still has errors that need manual edits.")

    return result


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    nc = args.no_color

    if not args.files:
        parser.print_help()
        sys.exit(0)

    if args.output and len(args.files) > 1:
        print("Error: --output can only be used with a single input file.", file=sys.stderr)
        sys.exit(2)

    any_errors = False

    for path in args.files:
        if path == "-":
            text = sys.stdin.read()
            result = process_file("<stdin>", text, args, source_path="<stdin>")
        else:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    text = fh.read()
            except FileNotFoundError:
                print(red(f"  ✖ File not found: {path}", nc), file=sys.stderr)
                any_errors = True
                continue
            except OSError as exc:
                print(red(f"  ✖ Cannot read {path}: {exc}", nc), file=sys.stderr)
                any_errors = True
                continue
            result = process_file(path, text, args, source_path=path)

        if not result.valid:
            any_errors = True
        elif args.strict and any(i.severity == "warning" for i in result.issues):
            any_errors = True

    if not args.summary:
        print()

    if any_errors:
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
