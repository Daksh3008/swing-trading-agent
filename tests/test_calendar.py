"""E3: the exchange calendar, holidays file and close cut-off."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from sta.core.market_calendar import (
    is_session,
    latest_closed_session,
    load_holidays,
    session_cutoff,
    sessions_behind,
    sessions_between,
)

HOLIDAYS = frozenset({date(2026, 11, 10), date(2026, 11, 24)})


def test_weekends_and_declared_holidays_are_not_sessions():
    assert is_session(date(2026, 11, 18), HOLIDAYS)
    assert not is_session(date(2026, 11, 10), HOLIDAYS)      # Diwali Balipratipada
    assert not is_session(date(2026, 11, 24), HOLIDAYS)      # Guru Nanak Jayanti
    assert not is_session(date(2026, 11, 21), HOLIDAYS)      # Saturday
    assert not is_session(date(2026, 11, 22), HOLIDAYS)      # Sunday


def test_latest_closed_session_respects_the_cutoff_and_looks_back():
    # 10 Nov is a holiday; before and after the cutoff it resolves to 9 Nov
    assert latest_closed_session(datetime(2026, 11, 10, 10, 0), HOLIDAYS) == date(2026, 11, 9)
    assert latest_closed_session(datetime(2026, 11, 10, 18, 0), HOLIDAYS) == date(2026, 11, 9)
    # before 16:00 the current session is still open
    assert latest_closed_session(datetime(2026, 11, 19, 11, 0), HOLIDAYS) == date(2026, 11, 18)
    assert latest_closed_session(datetime(2026, 11, 19, 16, 0), HOLIDAYS) == date(2026, 11, 19)
    # a Sunday walks back to Friday
    assert latest_closed_session(datetime(2026, 11, 22, 18, 0), HOLIDAYS) == date(2026, 11, 20)


def test_sessions_behind_skips_holidays():
    assert sessions_behind(date(2026, 11, 6), date(2026, 11, 12), HOLIDAYS) == 3
    assert sessions_behind(date(2026, 11, 17), date(2026, 11, 19), HOLIDAYS) == 2
    assert sessions_behind(date(2026, 11, 19), date(2026, 11, 19), HOLIDAYS) == 0
    assert sessions_behind(None, date(2026, 11, 19), HOLIDAYS) == 10_000


def test_sessions_between_is_half_open():
    assert sessions_between(date(2026, 11, 17), date(2026, 11, 19), HOLIDAYS) == [
        date(2026, 11, 18), date(2026, 11, 19)]
    assert sessions_between(date(2026, 11, 19), date(2026, 11, 19), HOLIDAYS) == []


def test_cutoff_is_four_pm():
    assert session_cutoff(date(2026, 11, 18)) == datetime(2026, 11, 18, 16, 0)


def test_bundled_holiday_file_covers_2026():
    days = load_holidays()
    assert date(2026, 11, 10) in days
    assert date(2026, 11, 24) in days
    assert date(2026, 12, 25) in days
    assert date(2026, 11, 18) not in days        # a normal trading day
    assert len(days) >= 15


def test_holiday_file_is_reloaded_when_it_changes(tmp_path):
    path = tmp_path / "holidays.txt"
    path.write_text("2026-11-18  # test\n", encoding="utf-8")
    first = load_holidays(path)
    assert date(2026, 11, 18) in first

    with open(path, "a", encoding="utf-8") as fh:
        fh.write("2026-11-19\n")
    second = load_holidays(path)
    assert date(2026, 11, 19) in second


def test_bad_lines_are_ignored(tmp_path):
    path = tmp_path / "holidays.txt"
    path.write_text("# comment\n\nnot-a-date\n2026-12-25\n", encoding="utf-8")
    assert load_holidays(path) == frozenset({date(2026, 12, 25)})


def test_missing_file_behaves_like_no_holidays(tmp_path):
    assert load_holidays(tmp_path / "absent.txt") == frozenset()


@pytest.mark.parametrize("day", [date(2026, 11, 18), date(2026, 11, 19)])
def test_normal_weekdays_are_sessions(day):
    assert is_session(day, frozenset())
