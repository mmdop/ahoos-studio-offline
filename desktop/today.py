"""Today, and how an answer looks -- shared by the engine (projects/nimbus-2-apex-live/modal/engine.py)
and the offline studio's agent (desktop/agent.py).

The model's knowledge stops before today: it is told the date (in both calendars), and a question
about now -- news, prices, today -- is one it searches before it answers. The studios draw more than
Markdown (desktop/ui/md.js): tables that turn, charts, the parts of a message; FORMAT tells it how.
"""

from __future__ import annotations

import re
import time

# The studio draws more than Markdown (desktop/ui/md.js); asked for with `nimbus_format` by both studios.
FORMAT = """# How your answers look
The studio shows rich formatting; use it when it makes the answer clearer, not for its own sake:
- Tables for comparisons and lists of things with properties (Markdown tables). For one thing's properties, a
  table of two columns, or put :::vertical on the line before the table and ::: after it to show it turned.
- Charts for numbers over time or across items: a fenced block with the language `chart` holding JSON:
  {"type": "bar" | "line" | "area" | "pie" | "donut" | "hbar", "title": "...", "unit": "...",
   "labels": ["...", "..."], "series": [{"name": "...", "data": [1, 2]}]}  -- use only real numbers you have.
- Parts of a long answer: :::section Title ... ::: around each part; :::steps around a numbered how-to list;
  :::details Title ... ::: for what most readers can skip.
- Callouts: :::note, :::tip, :::warning or :::danger, an optional title on the same line, closed with :::.
- Headings, bold for the key point, and code in fenced blocks with their language."""

# What the model is asked when the question is about now: it searches before it answers.
TIMELY = re.compile(
    r"\b(today|tonight|now|latest|news|headlines?|current(ly)?|this (week|month|year)|yesterday|tomorrow|price|"
    r"rate|weather|score|released?|20[2-3]\d)\b|"
    r"امروز|امشب|الان|اکنون|اخبار|خبر|تازه|جدید|آخرین|فعلی|این هفته|این ماه|امسال|دیروز|فردا|قیمت|نرخ|دلار|"
    r"یورو|طلا|سکه|بورس|بیت\s?کوین|هوا|نتیجه(ی)? بازی|۱۴۰[۰-۹]",
    re.I)

PERSIAN_MONTHS = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور", "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"]


def jalali(y: int, m: int, d: int) -> tuple[int, int, int]:
    """The Persian (solar hijri) date of a Gregorian one -- the arithmetic of the common converters."""
    days_before = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy = y - 1600
    days = 365 * gy + (gy + 3) // 4 - (gy + 99) // 100 + (gy + 399) // 400 + days_before[m - 1] + d - 1
    if m > 2 and (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)):
        days += 1
    days -= 79
    cycles, days = divmod(days, 12053)
    jy = 979 + 33 * cycles + 4 * (days // 1461)
    days %= 1461
    if days >= 366:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    jm = days // 31 if days < 186 else 6 + (days - 186) // 30
    jd = days % 31 + 1 if days < 186 else (days - 186) % 30 + 1
    return jy, jm + 1, jd


def today_text(now: float | None = None, local: bool = False) -> str:
    """The day and time -- in Tehran, or on this computer (local) -- said in a line that begins with a
    blank line, for a system text."""
    if local:
        t, where = time.localtime(now), "on this computer"
    else:
        t, where = time.gmtime((now if now is not None else time.time()) + 3.5 * 3600), "in Tehran"
    jy, jm, jd = jalali(t.tm_year, t.tm_mon, t.tm_mday)
    return (f"\n\nToday is {time.strftime('%A, %d %B %Y', t)} (in the Persian calendar {jd} {PERSIAN_MONTHS[jm - 1]} {jy}); "
            f"the time {where} is {time.strftime('%H:%M', t)}.")


def timely(request: str) -> bool:
    return bool(TIMELY.search(request or ""))
