from datetime import datetime

import pytest

from showroom_keeper.schedule import describe, next_time, parse_slots, previous_time
from showroom_keeper.status import JST

MONDAY = parse_slots("mon", "0:00,3:00,12:00,15:00,18:00")


def at(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=JST).timestamp()


def jst(ts):
    return datetime.fromtimestamp(ts, JST).strftime("%Y-%m-%d %H:%M")


def test_monday_schedule():
    # 2026-10-19 は月曜日
    assert jst(next_time(at("2026-10-16 10:00"), MONDAY)) == "2026-10-19 00:00"
    assert jst(next_time(at("2026-10-19 00:00"), MONDAY)) == "2026-10-19 03:00"  # ちょうどの時刻は済んだもの
    assert jst(next_time(at("2026-10-19 12:30"), MONDAY)) == "2026-10-19 15:00"
    assert jst(next_time(at("2026-10-19 18:00"), MONDAY)) == "2026-10-26 00:00"  # 月曜の最後のあとは翌週
    assert jst(previous_time(at("2026-10-16 10:00"), MONDAY)) == "2026-10-12 18:00"
    assert jst(previous_time(at("2026-10-19 14:59"), MONDAY)) == "2026-10-19 12:00"
    assert describe(MONDAY) == "毎週月曜日 0:00・3:00・12:00・15:00・18:00"


def test_parse_other_forms():
    assert parse_slots("月、木", "9:30") == [(0, 9, 30), (3, 9, 30)]
    assert len(parse_slots("毎日", "0:00")) == 7 and describe(parse_slots("毎日", "6")) == "毎日 6:00"
    assert parse_slots("Monday", "1:00") == [(0, 1, 0)]
    for days, times in (("someday", "0:00"), ("mon", "25:00"), ("mon", "")):
        with pytest.raises(ValueError):
            parse_slots(days, times)
