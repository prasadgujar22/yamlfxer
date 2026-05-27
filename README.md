# yamlfxer

A Python CLI tool that validates YAML files and auto-corrects common mistakes.

## Features

**Validation**
- Parses with PyYAML and reports exact line/column errors with clear suggestions
- Warns on duplicate keys, `yes`/`no`/`on`/`off` boolean traps, octal-looking integers (`0755`), and long lines

**Auto-corrections**
- Windows line endings → Unix (CRLF → LF)
- Tab indentation → 2-space indentation
- Trailing whitespace removal
- Missing space after colon (`key:value` → `key: value`)
- Bare colon in values → quoted (`title: Hello: World` → `title: "Hello: World"`)
- Duplicate keys in the same mapping block (context-aware)
- Missing newline at end of file

## Installation

```bash
pip install pyyaml
```

## Usage

```bash
# Validate one or more files
python validator.py config.yaml

# Validate and fix in-place
python validator.py config.yaml --fix

# Save corrected output to a new file
python validator.py config.yaml --output fixed.yaml

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
