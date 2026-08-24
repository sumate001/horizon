"""Structured-logging keys must not collide with LogRecord's own attributes.

This exists because the bug bit twice. `logging` raises KeyError when `extra`
carries a name it already uses, and both times the offending call sat *outside*
a try block in a worker path — so a counter called "created" and a field called
"name" each failed the whole article, not just the log line. The failure looks
like an ingestion bug and reads nothing like a logging mistake.

Scanning the source is the only way to catch it: these lines only run when
something has already gone wrong, which is the one moment nobody is watching.
"""

import ast
import logging
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "horizon"

#: Everything LogRecord sets on itself, plus the two the Formatter adds later.
RESERVED = set(logging.LogRecord("n", 20, "p", 1, "m", None, None).__dict__) | {
    "message",
    "asctime",
}


def _extra_keys(path: Path) -> list[tuple[int, str]]:
    """Literal string keys of every `extra={...}` in one module."""
    found: list[tuple[int, str]] = []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg != "extra" or not isinstance(keyword.value, ast.Dict):
                continue
            for key in keyword.value.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    found.append((key.lineno, key.value))
    return found


@pytest.mark.parametrize(
    "path", sorted(PACKAGE.rglob("*.py")), ids=lambda p: str(p.relative_to(PACKAGE))
)
def test_no_log_extra_key_shadows_a_logrecord_attribute(path: Path):
    clashes = [(line, key) for line, key in _extra_keys(path) if key in RESERVED]
    assert not clashes, (
        f"{path.relative_to(PACKAGE)} logs reserved key(s) {clashes}. "
        "logging raises KeyError on these — rename to entity_name, event_name, etc."
    )


def test_the_scanner_would_actually_catch_one(tmp_path: Path):
    """Guard the guard: a scanner that finds nothing must still be looking."""
    offender = tmp_path / "offender.py"
    offender.write_text('log.info("x", extra={"name": 1, "safe": 2})\n', encoding="utf-8")
    assert [key for _, key in _extra_keys(offender) if key in RESERVED] == ["name"]
