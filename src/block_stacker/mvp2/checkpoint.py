"""Utilities for locating checkpoints in fresh/ and played/.

Checkpoint filename conventions
--------------------------------
New format (since 2026-06):
    sac_YYYYMMDD-HHMMSS_<steps>_steps.zip
    Example: sac_20260627-143022_3990_steps.zip
    The run-timestamp prefix is shared across all checkpoints of the same training
    run (same 5 files within one run share the same YYYYMMDD-HHMMSS string).

Old format (backward compat, pre-2026-06):
    sac_<steps>_steps.zip
    Example: sac_3990_steps.zip
    Treated as run_ts = '00000000-000000', so they always sort before new-format files.

Sort key: (run_ts_str, steps) ascending.
"Latest" checkpoint = (run_ts_str, steps) maximum = newest run, highest step within run.
"""
from __future__ import annotations

import re
from pathlib import Path

# New format: sac_YYYYMMDD-HHMMSS_<steps>_steps.zip
_NEW_RE = re.compile(r"^sac_(\d{8}-\d{6})_(\d+)_steps\.zip$")
# Old format (backward compat): sac_<steps>_steps.zip
_OLD_RE = re.compile(r"^sac_(\d+)_steps\.zip$")
# Sentinel run_ts for old-format files; always sorts before any real timestamp
_OLD_TS = "00000000-000000"


def _parse_checkpoint_name(name: str) -> tuple[str, int] | None:
    """Parse a checkpoint filename → (run_ts_str, steps), or None if unrecognised.

    New format: sac_YYYYMMDD-HHMMSS_<steps>_steps.zip → (ts, steps)
    Old format: sac_<steps>_steps.zip                  → ('00000000-000000', steps)
    """
    m = _NEW_RE.match(name)
    if m:
        return (m.group(1), int(m.group(2)))
    m = _OLD_RE.match(name)
    if m:
        return (_OLD_TS, int(m.group(1)))
    return None


def find_latest_checkpoint(output_dir: Path) -> Path | None:
    """Return the checkpoint from the newest run with the highest step count.

    Searches fresh/ and played/ under output_dir.
    Sort key: (run_ts_str, steps) descending — newest run wins; within the same
    run, highest step wins.
    Old-format files (sentinel ts '00000000-000000') always lose to new-format.
    Returns None if no valid checkpoints exist in either directory.
    """
    best: tuple[str, int, Path] | None = None
    for subdir in ("fresh", "played"):
        d = output_dir / subdir
        if not d.exists():
            continue
        for p in d.glob("sac_*.zip"):
            parsed = _parse_checkpoint_name(p.name)
            if parsed is None:
                continue
            ts, steps = parsed
            if best is None or (ts, steps) > (best[0], best[1]):
                best = (ts, steps, p)
    return best[2] if best is not None else None


def list_checkpoints_sorted(
    output_dir: Path, subdir: str
) -> list[tuple[str, int, Path]]:
    """Return (run_ts_str, steps, path) tuples from subdir, sorted ascending.

    Sort order: (run_ts_str, steps) ascending — oldest run first, lowest step first.
    Old-format files ('00000000-000000') sort before any new-format file.
    """
    results: list[tuple[str, int, Path]] = []
    d = output_dir / subdir
    if not d.exists():
        return results
    for p in d.glob("sac_*.zip"):
        parsed = _parse_checkpoint_name(p.name)
        if parsed is None:
            continue
        ts, steps = parsed
        results.append((ts, steps, p))
    results.sort()
    return results
