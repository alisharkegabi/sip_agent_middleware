"""Deterministic Egyptian Arabic number/date formatting.

Mirrors the KB:NUMBERS procedure the ElevenLabs agent currently runs live
on every turn. Values here are precomputed once, at call-init time, and
sent as extra dynamic_variables so the agent reads a ready string instead
of doing the digit-to-words conversion itself.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Union

_TABLE_A = {
    1: "الف", 2: "الفين", 3: "تلات تلاف", 4: "اربع تلاف", 5: "خمس تلاف",
    6: "ست تلاف", 7: "سبع تلاف", 8: "تمن تلاف", 9: "تسع تلاف", 10: "عشر تلاف",
}

_TABLE_B = {
    1: "مية", 2: "متين", 3: "تلتمية", 4: "ربعمية", 5: "خمسمية",
    6: "ستمية", 7: "سبعمية", 8: "تمنمية", 9: "تسعمية",
}

_TABLE_C = {
    1: "واحد", 2: "اتنين", 3: "تلاتة", 4: "اربعة", 5: "خمسة", 6: "ستة",
    7: "سبعة", 8: "تمانية", 9: "تسعة", 10: "عشرة",
    11: "حداشر", 12: "اتناشر", 13: "تلتاشر", 14: "اربعتاشر", 15: "خمستاشر",
    16: "ستاشر", 17: "سبعتاشر", 18: "تمنتاشر", 19: "تسعتاشر", 20: "عشرين",
    30: "تلاتين", 40: "اربعين", 50: "خمسين", 60: "ستين", 70: "سبعين",
    80: "تمانين", 90: "تسعين",
}

_MONTHS = {
    1: "يناير", 2: "فبراير", 3: "مارس", 4: "ابريل", 5: "مايو", 6: "يونيو",
    7: "يوليو", 8: "اغسطس", 9: "سبتمبر", 10: "اكتوبر", 11: "نوفمبر", 12: "ديسمبر",
}


def _table_c(n: int) -> str:
    if n == 0:
        return ""
    if n in _TABLE_C:
        return _TABLE_C[n]
    tens, unit = divmod(n, 10)
    return f"{_TABLE_C[unit]} و{_TABLE_C[tens * 10]}"


def _table_a(n: int) -> str:
    if n in _TABLE_A:
        return _TABLE_A[n]
    # 11..99 thousand: table-C word for n, plus singular الف (KB:NUMBERS rule).
    return f"{_table_c(n)} الف"


def year_to_arabic_words(year: int) -> str:
    """Computed, not looked up -- KB:NUMBERS' fixed year table only ever
    covered 2024-2030 and would need manual upkeep every year. A year is
    just thousands+hundreds+tens/units spoken the same way as any other
    number, minus جنيه, with "ألفين" (year-specific spelling) for the
    2000s instead of number_to_arabic_words' "الفين"."""
    thousands, remainder = divmod(year, 1000)
    if thousands == 2:
        thousands_word = "ألفين"
    elif thousands == 1:
        thousands_word = "الف"
    else:
        thousands_word = _table_a(thousands)

    if remainder == 0:
        return thousands_word

    b = (remainder // 100) % 10
    c = remainder % 100
    parts = [thousands_word]
    if b:
        parts.append(_TABLE_B[b])
    if c:
        parts.append(_table_c(c))
    return " و".join(parts)


def number_to_arabic_words(value: Union[str, int, float], is_money: bool = True) -> str:
    """Implements KB:NUMBERS' clean -> split -> lookup -> join procedure."""
    cleaned = str(value).replace(",", "").split(".")[0].strip()
    n = int(cleaned)
    if n == 0:
        return "صفر جنيه" if is_money else "صفر"

    c = n % 100
    b = (n // 100) % 10
    a = n // 1000

    parts = []
    if a:
        parts.append(_table_a(a))
    if b:
        parts.append(_TABLE_B[b])
    if c:
        parts.append(_table_c(c))

    result = " و".join(parts)
    return f"{result} جنيه" if is_money else result


def _parse_ddmmyyyy(value: str) -> date:
    return datetime.strptime(value.strip(), "%d/%m/%Y").date()


def date_to_arabic_words(value: Union[str, date]) -> str:
    """value: a date object, or a 'DD/MM/YYYY' string (matches this
    service's existing dynamic_variables format, e.g. due_date, Today_date)."""
    d = value if isinstance(value, date) else _parse_ddmmyyyy(value)
    return f"يوم {_table_c(d.day)} {_MONTHS[d.month]} {year_to_arabic_words(d.year)}"


def compute_day_window(today_str: str) -> dict:
    """DAY0/DAY1/DAY2 per Section 9 -- today, tomorrow, day after, both as
    raw DD/MM/YYYY (for the agent's accept/reject comparison) and spoken
    (for the confirmation line). This is also the window for partial/split
    payment plans -- splitting the amount does not extend the deadline."""
    day0 = _parse_ddmmyyyy(today_str)
    day1 = day0 + timedelta(days=1)
    day2 = day0 + timedelta(days=2)
    return {
        "day0_date": day0.strftime("%d/%m/%Y"),
        "day0_spoken": date_to_arabic_words(day0),
        "day1_date": day1.strftime("%d/%m/%Y"),
        "day1_spoken": date_to_arabic_words(day1),
        "day2_date": day2.strftime("%d/%m/%Y"),
        "day2_spoken": date_to_arabic_words(day2),
    }


# dynamic_variables key -> (spoken key, is_money) for the simple 1:1 fields.
_AMOUNT_FIELDS = {
    "payment_amount": ("amount_spoken", True),
    "outstanding_balance": ("balance_spoken", True),
    "total_loan_amount": ("total_spoken", True),
    "last_paid_amount": ("lastpaid_spoken", True),
    "remain_installments": ("remaining_spoken", False),
    "total_number_installments": ("total_count_spoken", False),
}

_DATE_FIELDS = {
    "due_date": "due_date_spoken",
    "last_payment_date": "lastdate_spoken",
}


def enrich_dynamic_variables(dynamic_variables: dict, logger=None) -> dict:
    """Adds *_spoken (and day0/1/2) fields to a copy of dynamic_variables.
    Never raises: a field that's missing or malformed is skipped and logged,
    so a bad/absent value degrades to "the agent doesn't get that particular
    spoken variable" rather than failing the whole call."""
    out = dict(dynamic_variables)

    for src_key, (dst_key, is_money) in _AMOUNT_FIELDS.items():
        if src_key in dynamic_variables:
            try:
                out[dst_key] = number_to_arabic_words(dynamic_variables[src_key], is_money=is_money)
            except (ValueError, KeyError) as e:
                if logger:
                    logger.warning(f"could not convert {src_key}={dynamic_variables[src_key]!r} to words: {e}")

    for src_key, dst_key in _DATE_FIELDS.items():
        if src_key in dynamic_variables:
            try:
                out[dst_key] = date_to_arabic_words(dynamic_variables[src_key])
            except (ValueError, KeyError) as e:
                if logger:
                    logger.warning(f"could not convert {src_key}={dynamic_variables[src_key]!r} to words: {e}")

    if "Today_date" in dynamic_variables:
        try:
            out.update(compute_day_window(dynamic_variables["Today_date"]))
        except (ValueError, KeyError) as e:
            if logger:
                logger.warning(f"could not compute day window from Today_date={dynamic_variables['Today_date']!r}: {e}")

    return out
