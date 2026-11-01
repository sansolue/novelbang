"""Daily writing progress: words written each day, a streak, and the pace a deadline needs.

Only writing counts: each time the editor saves a chapter, the change in its word
count is added to today's total. Importing, restoring a version, replacing across
the book and deleting chapters don't count, so they can't fake a good day or spoil
one. Cuts made while writing do count against the day: it's the net words added.

Kept in .novelbang/progress.json, one total per day (local date):

    {"days": {"2026-11-01": 812, "2026-11-02": 455}}
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from .project import META_DIR, Project

PROGRESS = "progress.json"
CHART_DAYS = 30


def _path(p: Project) -> Path:
    return p.root / META_DIR / PROGRESS


def load_days(p: Project) -> dict[str, int]:
    try:
        days = json.loads(_path(p).read_text("utf-8")).get("days", {})
    except (OSError, ValueError, AttributeError):
        return {}
    return {d: n for d, n in days.items() if isinstance(n, int) and not isinstance(n, bool)}


def record(p: Project, words: int, today: date | None = None) -> None:
    """Add `words` (negative for cuts) to today's total."""
    if not words:
        return
    day = (today or date.today()).isoformat()
    days = load_days(p)
    days[day] = days.get(day, 0) + words
    p._write_meta(PROGRESS, {"days": days})


def streak(days: dict[str, int], goal: int, today: date) -> int:
    """Days in a row the goal was met (any words, with no goal), up to today.
    Today not done yet doesn't break it: the streak runs to yesterday until the day ends."""
    met = lambda d: days.get(d.isoformat(), 0) >= max(goal, 1)  # noqa: E731
    day = today if met(today) else today - timedelta(days=1)
    n = 0
    while met(day):
        n, day = n + 1, day - timedelta(days=1)
    return n


def summary(p: Project, total_words: int, today: date | None = None) -> dict:
    today = today or date.today()
    days = load_days(p)
    goal = p.meta.daily_words
    out = {
        "today": days.get(today.isoformat(), 0),
        "goal": goal,
        "streak": streak(days, goal, today),
        "recent": [
            {"date": d.isoformat(), "words": days.get(d.isoformat(), 0)}
            for d in (today - timedelta(days=i) for i in range(CHART_DAYS - 1, -1, -1))
        ],
        "deadline": None,
    }
    if p.meta.deadline:
        left = (date.fromisoformat(p.meta.deadline) - today).days + 1  # today counts
        remaining = max(p.meta.target_words - total_words, 0)
        # Today's words are already in the total, so the pace is spread over today and after.
        out["deadline"] = {
            "date": p.meta.deadline,
            "days_left": max(left, 0),
            "remaining": remaining,
            "per_day": -(-remaining // left) if left > 0 else None,
        }
    return out
