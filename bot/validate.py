"""Input validation. Everything a user types passes through here before it reaches the DB
or an outbound URL.

Invalid carries a user-facing Chinese message, so the conversation layer can hand the text
straight back without composing an error of its own.
"""

import re

ROOM_RE = re.compile(r"^\d{3}$")
# 身分證 A123456789 / 居留證 AB12345678 / 病歷號 or passport, first ten characters.
PATIENT_ID_RE = re.compile(r"^(?:[A-Za-z]\d{9}|[A-Za-z]{2}\d{8}|\d{6,10}|[A-Za-z0-9]{10})$")
BIRTH_RE = re.compile(r"^(\d{1,2})[/-]?(\d{1,2})$")

# Full-width digits are what a phone keyboard produces surprisingly often.
FULLWIDTH = str.maketrans("０１２３４５６７８９", "0123456789")


class Invalid(ValueError):
    """Raised with a message meant to be shown to the user as-is."""


def normalize(text):
    return (text or "").translate(FULLWIDTH).strip()


def room(value):
    value = normalize(value)
    if not ROOM_RE.match(value):
        raise Invalid("診間號碼是 3 位數字，例如 115。")
    return value


def ticket_no(value):
    value = normalize(value)
    if not value.isdigit() or not (1 <= int(value) <= 999):
        raise Invalid("看診號請輸入 1-999 的數字。")
    return int(value)


def patient_id(value):
    """Shape check only, and deliberately local: a malformed identifier is rejected here
    and never leaves the machine."""
    value = normalize(value)
    if not PATIENT_ID_RE.match(value):
        raise Invalid("證號格式看起來不對，請確認後再試一次。")
    return value.upper()


def birth_md(value):
    """Birthday month/day, as the hospital's reg62 expects it (sMonth / sDay)."""
    m = BIRTH_RE.match(normalize(value))
    if not m:
        raise Invalid("生日請輸入月日，例如 0304。")
    month, day = int(m.group(1)), int(m.group(2))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        raise Invalid("生日的月份或日期超出範圍。")
    return "%02d" % month, "%02d" % day


def notify_ahead(value, default=5):
    value = normalize(value)
    if not value:
        return default
    if not value.isdigit() or not (0 <= int(value) <= 50):
        raise Invalid("提前提醒的人數請輸入 0-50。")
    return int(value)
