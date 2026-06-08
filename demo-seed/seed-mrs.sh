#!/usr/bin/env bash
# Seed the gitlab.com demo project with branches and MRs so glmon screenshots
# have realistic variety. Run AFTER the initial commit on main has been pushed.
#
# Requires: glab CLI authenticated against gitlab.com.
# Run from the root of the cloned glmon-demo repo.

set -euo pipefail

require() { command -v "$1" >/dev/null || { echo "missing: $1" >&2; exit 1; }; }
require git
require glab

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "not inside a git repo" >&2
  exit 1
fi

if [[ "$(git rev-parse --abbrev-ref HEAD)" != "main" ]]; then
  echo "checkout main first" >&2
  exit 1
fi

echo "==> branch 1/3: feature/add-modulo (passes, ready to merge)"
git checkout -b feature/add-modulo
cat > src/calculator.py <<'PY'
def add(a: int, b: int) -> int:
    return a + b


def subtract(a: int, b: int) -> int:
    return a - b


def multiply(a: int, b: int) -> int:
    return a * b


def divide(a: int, b: int) -> float:
    if b == 0:
        raise ZeroDivisionError("cannot divide by zero")
    return a / b


def modulo(a: int, b: int) -> int:
    if b == 0:
        raise ZeroDivisionError("cannot modulo by zero")
    return a % b
PY
cat >> tests/test_calculator.py <<'PY'


def test_modulo():
    from src.calculator import modulo
    assert modulo(10, 3) == 1
PY
git add -A
git commit -m "feat: add modulo operation"
git push -u origin feature/add-modulo
glab mr create \
  --title "Add modulo operation" \
  --description "Adds modulo() to the calculator module with a divide-by-zero guard." \
  --target-branch main \
  --source-branch feature/add-modulo \
  --yes

echo "==> branch 2/3: feature/divide-bug (integration test fails)"
git checkout main
git checkout -b feature/divide-bug
# Introduce a bug: divide returns 0 instead of raising
cat > src/calculator.py <<'PY'
def add(a: int, b: int) -> int:
    return a + b


def subtract(a: int, b: int) -> int:
    return a - b


def multiply(a: int, b: int) -> int:
    return a * b


def divide(a: int, b: int) -> float:
    if b == 0:
        return 0.0
    return a / b
PY
cat >> tests/test_calculator.py <<'PY'


def test_integration_divide_pipeline():
    from src.calculator import divide
    with pytest.raises(ZeroDivisionError):
        divide(10, 0)
PY
git add -A
git commit -m "refactor: soften divide-by-zero handling"
git push -u origin feature/divide-bug
glab mr create \
  --title "Soften divide-by-zero handling" \
  --description "Returns 0 instead of raising. **Note: integration test still expects raise** — needs to update test or revert." \
  --target-branch main \
  --source-branch feature/divide-bug \
  --draft \
  --yes

echo "==> branch 3/3: feature/loose-typing (mypy fails)"
git checkout main
git checkout -b feature/loose-typing
cat >> src/calculator.py <<'PY'


def chain_ops(a, b, c):
    return add(multiply(a, b), c)
PY
git add -A
git commit -m "feat: add chain_ops helper"
git push -u origin feature/loose-typing
glab mr create \
  --title "Add chain_ops helper" \
  --description "Convenience wrapper for multiply-then-add." \
  --target-branch main \
  --source-branch feature/loose-typing \
  --yes

git checkout main

echo
echo "Done. 3 MRs opened. Pipelines will run automatically."
echo "Trigger fresh pipelines anytime with: git commit --allow-empty -m 'rerun ci' && git push"
