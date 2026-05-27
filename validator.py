#!/usr/bin/env python3
"""
YAML Validator and Corrector
Validates YAML input and attempts to auto-correct common mistakes.
"""

import sys
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
    """Replace tab indentation with 2 spaces."""
    lines = text.splitlines(keepends=True)
    changed = False
    out = []
    for i, line in enumerate(lines, 1):
        stripped = line.lstrip("\t")
        n_tabs = len(line) - len(stripped)
        if n_tabs:
            line = "  " * n_tabs + stripped
            changed = True
        out.append(line)
    if changed:
        result.corrections_made.append("Replaced tab indentation with 2-space indentation")
    return "".join(out)


def fix_missing_space_after_colon(text: str, result: ValidationResult) -> str:
    """Add space after colon in key:value pairs where missing."""
    pattern = re.compile(r'^(\s*[\w\-\.]+):([^\s\n\r/])', re.MULTILINE)
    def replacer(m):
        return m.group(1) + ": " + m.group(2)
    new_text, n = pattern.subn(replacer, text)
    if n:
        result.corrections_made.append(
            f"Added missing space after colon in {n} key-value pair(s)"
        )
    return new_text


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
    # Group 1: key + ": " prefix (no newline allowed inside)
    # Group 2: the value (no newline allowed)
    pattern = re.compile(r'^([ \t]*[\w\-\.]+[ \t]*:[ \t]+)([^\n]+)$', re.MULTILINE)

    def replacer(m):
        val = m.group(2).strip()
        # skip already-quoted, block scalars, booleans, nulls, numbers
        if (val.startswith(('"', "'", '|', '>', '{', '[', '#'))
                or val in ('true', 'false', 'null', 'True', 'False', 'Null',
                           '~', 'yes', 'no', 'on', 'off',
                           'YES', 'NO', 'ON', 'OFF', 'Yes', 'No', 'On', 'Off')
                or re.match(r'^-?\d', val)):
            return m.group(0)
        # only quote if there's a colon-space sequence in the value
        if ': ' in val:
            return m.group(1) + '"' + val.replace('\\', '\\\\').replace('"', '\\"') + '"'
        return m.group(0)

    count = 0
    def counting_replacer(m):
        nonlocal count
        original = m.group(0)
        replaced = replacer(m)
        if replaced != original:
            count += 1
        return replaced

    new_text = pattern.sub(counting_replacer, text)
    if count:
        result.corrections_made.append(
            f"Quoted {count} value(s) that contained unescaped colons"
        )
    return new_text


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


# ─────────────────────────────────────────────
# Core validate + correct pipeline
# ─────────────────────────────────────────────

CORRECTORS = [
    fix_windows_newlines,
    fix_tabs,
    fix_trailing_whitespace,
    fix_missing_space_after_colon,
    fix_unquoted_colon_in_value,
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


def validate_and_correct(text: str) -> ValidationResult:
    result = ValidationResult(valid=False)
    working = text

    # First pass: try to parse as-is
    try:
        list(yaml.safe_load_all(working))
        result.valid = True
        lint(working, result)
        # Still run non-destructive cosmetic fixes even on valid YAML
        cosmetic = [fix_windows_newlines, fix_tabs, fix_trailing_whitespace, fix_missing_newline_at_eof]
        for fix in cosmetic:
            working = fix(working, result)
        if result.corrections_made:
            result.corrected_yaml = working
        return result
    except yaml.YAMLError:
        pass

    # Correction loop — run all correctors up to MAX_ATTEMPTS times until
    # the document parses cleanly.  We collect corrections_made in a local
    # list and deduplicate at the end so repeated passes don't spam the log.
    all_corrections: list[str] = []
    all_issues: list[Issue] = []

    for attempt in range(MAX_ATTEMPTS):
        pass_result = ValidationResult(valid=False)
        for fix in CORRECTORS:
            working = fix(working, pass_result)
        all_corrections.extend(pass_result.corrections_made)
        all_issues.extend(i for i in pass_result.issues if i.severity != "error")

        try:
            list(yaml.safe_load_all(working))
            result.valid = True
            result.corrections_made = _dedup_ordered(all_corrections)
            result.issues = all_issues
            lint(working, result)
            result.corrected_yaml = working
            return result
        except yaml.YAMLError:
            pass  # keep trying

    # Failed to correct — report the final parse error
    result.corrections_made = _dedup_ordered(all_corrections)
    result.issues = all_issues
    try:
        list(yaml.safe_load_all(working))
    except yaml.YAMLError as exc:
        result.issues.insert(0, parse_yaml_error(exc))

    result.corrected_yaml = working  # return best-effort output anyway
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
              yaml-validator config.yaml --fix --output fixed.yaml
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
        help="Write corrected YAML back to the original file (in-place).",
    )
    p.add_argument(
        "--output", "-o",
        metavar="FILE",
        help="Write corrected YAML to this file (only valid with a single input file).",
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

    if warnings:
        print(yellow(f"\n  Warnings ({len(warnings)}):", nc))
        for issue in warnings:
            print(yellow(str(issue), nc))

    if infos:
        print(dim(f"\n  Info ({len(infos)}):", nc))
        for issue in infos:
            print(dim(str(issue), nc))

    if result.corrections_made:
        print(f"\n  {bold('Corrections applied:', nc)}")
        for c in result.corrections_made:
            print(f"    • {c}")

    if not result.issues and not result.corrections_made:
        print(f"  {dim('No issues found.', nc)}")

    return bool(errors)


def process_file(
    label: str,
    text: str,
    args: argparse.Namespace,
    source_path: Optional[str] = None,
) -> bool:
    """Validate one file and handle output/fix flags. Returns True on error."""
    result = validate_and_correct(text)
    has_errors = report_file(label, text, result, args)

    corrected = result.corrected_yaml

    if corrected and args.print_corrected:
        print(f"\n{'─' * 60}")
        print(f"  Corrected YAML ({label})")
        print(f"{'─' * 60}\n")
        print(corrected)

    if corrected and args.output and source_path:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(corrected)
        print(f"\n  Wrote corrected YAML → {args.output}")

    if corrected and args.fix and source_path and source_path != "<stdin>":
        if result.corrections_made:
            with open(source_path, "w", encoding="utf-8") as fh:
                fh.write(corrected)
            print(f"  Fixed in-place: {source_path}")
        else:
            print(f"  No changes needed: {source_path}")

    return has_errors


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
            had_error = process_file("<stdin>", text, args, source_path="<stdin>")
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
            had_error = process_file(path, text, args, source_path=path)

        if had_error:
            any_errors = True
        elif args.strict:
            result_tmp = validate_and_correct(text if path == "-" else open(path).read())
            if any(i.severity == "warning" for i in result_tmp.issues):
                any_errors = True

    if not args.summary:
        print()

    if any_errors:
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
