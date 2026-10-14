from showroom_keeper.status import format_status


def test_format_live():
    info = {"name": "部屋", "is_live": True, "started_at": 1_000_000, "viewers": 55256}
    s = format_status(info, now=1_000_000 + 3 * 3600 + 25 * 60)
    assert "視聴者数 55,256 人" in s and "経過 3 時間 25 分" in s


def test_format_offline():
    assert "配信していません" in format_status({"name": "部屋", "is_live": False, "started_at": None, "viewers": None})
