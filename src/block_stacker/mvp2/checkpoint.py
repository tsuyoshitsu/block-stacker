"""Utilities for locating checkpoints in fresh/ and played/."""
from __future__ import annotations

import re
from pathlib import Path


def find_latest_checkpoint(output_dir: Path) -> Path | None:
    """Return the highest-step checkpoint found in fresh/ or played/.

    Searches both directories and returns the path with the largest step count.
    Returns None if no checkpoints exist in either directory.
    """
    best: tuple[int, Path] | None = None
    for subdir in ("fresh", "played"):
        d = output_dir / subdir
        if not d.exists():
            continue
        for p in d.glob("sac_*_steps.zip"):
            m = re.match(r"^sac_(\d+)_steps\.zip$", p.name)
            if m:
                steps = int(m.group(1))
                if best is None or steps > best[0]:
                    best = (steps, p)
    return best[1] if best is not None else None


def list_checkpoints_sorted(output_dir: Path, subdir: str) -> list[tuple[int, Path]]:
    """Return sorted list of (steps, path) from the given subdir (fresh or played).

    Sorted ascending by step count (oldest first).
    """
    results: list[tuple[int, Path]] = []
    d = output_dir / subdir
    if not d.exists():
        return results
    for p in d.glob("sac_*_steps.zip"):
        m = re.match(r"^sac_(\d+)_steps\.zip$", p.name)
        if m:
            results.append((int(m.group(1)), p))
    results.sort()
    return results
