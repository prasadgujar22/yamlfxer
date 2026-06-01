# yamlfxer

A Python CLI tool that validates YAML files and auto-corrects common mistakes.

## Features

**Validation**
- Parses with PyYAML and reports exact line/column errors with clear suggestions
- Warns on duplicate keys, `yes`/`no`/`on`/`off` boolean traps, octal-looking integers (`0755`), and long lines

**Auto-corrections**
- Windows line endings → Unix (CRLF → LF)
- Tab indentation → 2-space indentation
- **Broken / inconsistent indentation** → re-indented to consistent 2-space levels
  (fixes odd widths like 1/3/5 spaces, mixed tabs+spaces, and under/over-indented
  keys or list items)
- Trailing whitespace removal
- Missing space after colon (`key:value` → `key: value`)
- **Missing colon entirely** (`name John` → `name: John`) — context-aware, see note below
- Bare colon in values → quoted (`title: Hello: World` → `title: "Hello: World"`)
- Duplicate keys in the same mapping block (context-aware)
- Missing newline at end of file

> **About indentation repair:** YAML indentation is *semantic*, so repair is
> necessarily heuristic. The tool reconstructs nesting from the relative indent
> of each line and re-emits clean 2-space levels. It only applies the rewrite if
> the result parses, so it will never replace your file with *unparseable* YAML —
> but for deeply ambiguous cases (especially sequences of mappings) the chosen
> structure may not match your intent, so review the generated `.fixed` file
> before using it.

> **About missing colons:** YAML reads `name John` as the plain string
> `"name John"` (and several such lines fold into a single scalar), so a missing
> colon can't be detected by parsing alone. To stay safe, the tool rewrites a
> colon-less `key value` line **only** when an explicit `key: value` mapping
> sibling already exists at the *same indentation level* — proof that a mapping
> was intended there. For example, in
>
> ```yaml
> name: John
> age 30      # ← fixed to "age: 30" because of the sibling above
> ```
>
> A standalone block of colon-less lines (no `key: value` anchor), a real scalar
> value under a block key, and lines whose value merely contains a colon are all
> left untouched — they're indistinguishable from valid YAML. The rewrite is
> additionally applied only if the result still parses.

## Installation

```bash
pip install pyyaml
```

## Usage

```bash
# Validate one or more files
python validator.py config.yaml

# Fix and write to a SEPARATE file (config.yaml -> config.fixed.yaml).
# The original file is never modified.
python validator.py config.yaml --fix

# Write the corrected output to a specific path
python validator.py config.yaml --output clean.yaml

# Print corrected YAML to stdout
python validator.py config.yaml --print-corrected

# Validate many files, one-line summary each
python validator.py *.yaml --summary

# Read from stdin
cat broken.yaml | python validator.py -

# Disable colour output (useful in CI)
python validator.py config.yaml --no-color

# Exit code 1 even on warnings (strict CI mode)
python validator.py config.yaml --strict
```

> **Note:** `--fix` always writes to a **separate** file (`<name>.fixed.<ext>`)
> and never overwrites your original. Use `--output` to choose the path.

## Exit codes

| Code | Meaning |
|------|---------|
| `0`  | Valid (no errors) |
| `1`  | Errors found (or warnings, with `--strict`) |
| `2`  | CLI usage error |

## Issue codes

| Code | Severity | Description |
|------|----------|-------------|
| `E001` | error   | YAML parse error |
| `W002` | warning | Duplicate key in same mapping block |
| `W003` | warning | Value treated as boolean in YAML 1.1 |
| `W004` | warning | Octal-looking integer |
| `W005` | warning | Missing space after colon (`key:value`) |
| `W006` | warning | Missing colon after key (`name John`) |
| `I001` | info    | Line longer than 120 characters |

## Example

Given this broken YAML:

```yaml
name: broken-config
version:1.2.3

server:
        host: localhost
        port: 8080
        tls: no

database:
  host: db.example.com   
  name: mydb
  name: duplicate-db

title: Hello: World
```

Running `python validator.py broken.yaml --print-corrected` produces:

```
  broken.yaml  ✔ valid

  Warnings (2):
  ⚠ [W002] line 13: Duplicate key 'name' (first seen at line 12)
  ⚠ [W003] line 7: 'no' is treated as a boolean in YAML 1.1

  Corrections applied:
    • Replaced tab indentation with 2-space indentation
    • Removed trailing whitespace
    • Added missing space after colon in 1 key-value pair(s)
    • Quoted 1 value(s) that contained unescaped colons
    • Removed 1 duplicate key block(s)
```

## Requirements

- Python 3.10+
- `pyyaml >= 6.0`
