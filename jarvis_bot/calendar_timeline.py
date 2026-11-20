"""Pure availability visuals; inputs contain only dates and busy intervals."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Sequence


def union_seconds(intervals: Sequence[tuple[datetime, datetime]]) -> float:
    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return sum((end - start).total_seconds() for start, end in merged)


def busy_cells(first: datetime, last: datetime,
               intervals: Sequence[tuple[datetime, datetime]]) -> str:
    """Use the union of exact intersections, including shortened final cells."""
    cells: list[str] = []
    cursor = first
    while cursor < last:
        end = min(cursor + timedelta(minutes=15), last)
        busy = union_seconds([(max(a, cursor), min(b, end))
                              for a, b in intervals if a < end and b > cursor])
        cells.append("█" if busy >= (end - cursor).total_seconds()
                     else "▓" if busy else "░")
        cursor = end
    return "".join(cells)


@dataclass(frozen=True)
class TimelineDay:
    first: datetime
    last: datetime
    intervals: tuple[tuple[datetime, datetime], ...]
    working: bool = True


def _clock(value: datetime) -> str:
    return value.strftime("%I:%M %p").lstrip("0")


def comparison_pages(days: Sequence[TimelineDay], *, timezone_name: str,
                     fetched_at: datetime) -> list[str]:
    """Compare up to five days per grid, paginating complete fenced rows.

    All days use the same configured local work hours. The 1500-character
    budget leaves room for the caller's multi-day totals on the first page.
    """
    pages: list[str] = []
    for offset in range(0, len(days), 5):
        columns = days[offset:offset + 5]
        first, last = columns[0].first, columns[0].last
        heading = (
            "### Side-by-side timeline\n"
            f"-# {columns[0].first:%Y-%m-%d}–{columns[-1].first:%Y-%m-%d} · {timezone_name}\n"
            "```text\n"
            + "    Time │ " + " │ ".join(f"{day.first:%a %d}".center(6) for day in columns)
            + "\n         │ " + " │ ".join(f"{day.first:%Z}".center(6) for day in columns)
            + "\n"
        )
        footer = (
            "\n```\n-# █ busy · ▓ partly busy · ░ free · — no work · 15m/block; rows up to 30m\n"
            f"-# Updated {fetched_at:%b %d} at {_clock(fetched_at)} {fetched_at:%Z} · Outlook publishing may lag."
        )
        rows: list[str] = []
        cursor = first
        while cursor < last:
            elapsed = cursor - first
            cells = []
            for day in columns:
                start = day.first + elapsed
                end = min(start + timedelta(minutes=30), day.last)
                blocks = busy_cells(start, end, day.intervals if day.working else ())
                cells.append((blocks if day.working else blocks.replace("░", "—")).center(6))
            rows.append(f"{_clock(cursor):>8} │ " + " │ ".join(cells))
            cursor = min(cursor + timedelta(minutes=30), last)
        rows.append(f"{_clock(last):>8} └ End")
        chunk: list[str] = []
        for row in rows:
            if chunk and len(heading + "\n".join(chunk + [row]) + footer) > 1500:
                pages.append(heading + "\n".join(chunk) + footer)
                chunk = []
            chunk.append(row)
        pages.append(heading + "\n".join(chunk) + footer)
    return pages
