"""
ui.py — Terminal progress helpers (no external dependencies).

All output goes to stdout so it interleaves cleanly with logging (stderr).
ANSI colours are used; they degrade gracefully on terminals that don't
support them — the text is still readable, just without colour.
"""

from __future__ import annotations

import sys

# ---------------------------------------------------------------------------
# ANSI codes
# ---------------------------------------------------------------------------
_BOLD   = "\033[1m"
_RESET  = "\033[0m"
_GREEN  = "\033[32m"
_RED    = "\033[31m"
_YELLOW = "\033[33m"
_CYAN   = "\033[36m"
_DIM    = "\033[2m"

_W = 60   # banner width


def _c(text: str, code: str) -> str:
    """Wrap text in an ANSI colour code."""
    return f"{code}{text}{_RESET}"


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def banner(title: str) -> None:
    """Print a bold section header."""
    line = "─" * _W
    print(f"\n{_BOLD}{line}{_RESET}")
    print(f"{_BOLD}  {title}{_RESET}")
    print(f"{_BOLD}{line}{_RESET}")


def step(msg: str, ok: bool = True, warn: bool = False) -> None:
    """Print a single bootstrap / progress step."""
    if warn:
        icon = _c("⚠", _YELLOW)
    elif ok:
        icon = _c("✓", _GREEN)
    else:
        icon = _c("✗", _RED)
    print(f"  {icon}  {msg}")


def section(title: str) -> None:
    """Print a lighter sub-section divider."""
    print(f"\n  {_CYAN}{_BOLD}{title}{_RESET}")
    print(f"  {'·' * (_W - 2)}")


def divider() -> None:
    print(f"  {_DIM}{'─' * (_W - 2)}{_RESET}")


# ---------------------------------------------------------------------------
# Task 1 — per-iteration row
# ---------------------------------------------------------------------------

def precision_row(
    idx: int,
    total: int,
    target_pan: float,
    target_tilt: float,
    actual_pan: float,
    actual_tilt: float,
    delta_pan: float,
    delta_tilt: float,
    passed: bool,
) -> None:
    status = _c("PASS", _GREEN) if passed else _c("FAIL", _RED)
    print(
        f"  [{idx:>3}/{total}]"
        f"  tgt=({target_pan:+7.2f}°,{target_tilt:+6.2f}°)"
        f"  act=({actual_pan:+7.2f}°,{actual_tilt:+6.2f}°)"
        f"  Δ=({delta_pan:.3f}°,{delta_tilt:.3f}°)"
        f"  {status}"
    )


def preset_create_row(idx: int, total: int, token: str, pan: float, tilt: float) -> None:
    print(
        f"  [Create {idx:>2}/{total}]"
        f"  token={_c(token, _CYAN)}"
        f"  pos=({pan:+7.2f}°,{tilt:+6.2f}°)"
    )


def preset_goto_row(
    idx: int, total: int, delta_pan: float, delta_tilt: float, passed: bool
) -> None:
    status = _c("PASS", _GREEN) if passed else _c("FAIL", _RED)
    print(
        f"  [GoTo   {idx:>2}/{total}]"
        f"  Δ=({delta_pan:.3f}°,{delta_tilt:.3f}°)"
        f"  {status}"
    )


# ---------------------------------------------------------------------------
# Task 2 — per-batch row
# ---------------------------------------------------------------------------

def latency_row(
    cycle: int,
    total: int,
    comm_ms: float | None,
    mech_ms: float | None,
    mean_comm_ms: float,
    passed: bool,
) -> None:
    status = _c("✓", _GREEN) if passed else _c("✗", _RED)
    comm_str = f"{comm_ms:6.1f}ms" if comm_ms is not None else "    n/a "
    mech_str = f"{mech_ms:7.1f}ms" if mech_ms is not None else "      n/a "
    print(
        f"  [{cycle:>4}/{total}]"
        f"  comm={comm_str}"
        f"  mech={mech_str}"
        f"  avg={mean_comm_ms:6.1f}ms"
        f"  {status}"
    )


# ---------------------------------------------------------------------------
# Summary tables
# ---------------------------------------------------------------------------

def summary_table(title: str, rows: list[tuple[str, str]]) -> None:
    """Print a simple two-column table."""
    divider()
    print(f"  {_BOLD}{title}{_RESET}")
    for label, value in rows:
        print(f"    {label:<38} {value}")


def final_banner(csv_path: str) -> None:
    line = "─" * _W
    print(f"\n{_BOLD}{line}{_RESET}")
    print(f"  {_c('Complete', _GREEN)}  →  {csv_path}")
    print(f"{_BOLD}{line}{_RESET}\n")
