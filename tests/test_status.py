from showroom_keeper.status import count_recent_commenters, format_status


def test_format_live():
    info = {"name": "部屋", "is_live": True, "started_at": 1_000_000, "excitement": 55256,
            "commenters": 15, "commenters_capped": False}
    s = format_status(info, now=1_000_000 + 3 * 3600 + 25 * 60)
    assert "盛り上がり度: 55,256" in s and "コメント人数: 15 人" in s and "経過 3 時間 25 分" in s
    assert "視聴者数" not in s


def test_format_offline():
    assert "配信していません" in format_status({"name": "部屋", "is_live": False, "started_at": None})


def test_count_recent_commenters():
    now = 10_000
    log = [{"user_id": 1, "created_at": now - 30}, {"user_id": 1, "created_at": now - 60},
           {"user_id": 2, "created_at": now - 500}, {"user_id": 3, "created_at": now - 700}]
    assert count_recent_commenters(log, now) == (2, False)
    busy = [{"user_id": i % 40, "created_at": now - i} for i in range(100)]  # 100 件が 100 秒に収まる
    assert count_recent_commenters(busy, now) == (40, True)


def test_format_realtime_viewers_fresh_and_stale():
    base = {"name": "部屋", "is_live": True, "started_at": None, "excitement": 1, "commenters": 0,
            "commenters_capped": False}
    fresh = format_status({**base, "live_stat": {"viewer_count": 42, "viewer_count_at": 1000}}, now=1060)
    assert "リアルタイム視聴者数: 42 人" in fresh
    stale = format_status({**base, "live_stat": {"viewer_count": 42, "viewer_count_at": 1000}}, now=2000)
    assert "リアルタイム視聴者数" not in stale
    err = format_status({**base, "live_stat": {"viewer_count": None, "viewer_count_error": "ログインが必要です。"}}, now=1)
    assert "取得できません (ログインが必要です。)" in err


def test_format_classic_and_active_users():
    base = {"name": "部屋", "is_live": True, "started_at": None, "excitement": 1, "commenters": 0,
            "commenters_capped": False}
    stat = {"broadcast_style": "classic", "viewer_count": None, "active_users": 37, "active_at": 1000,
            "active_observed": 900}
    s = format_status({**base, "live_stat": stat}, now=1030)
    assert "リアルタイム視聴者数: 非公開 (クラシック形式)" in s
    assert "推定アクティブ人数: 37 人以上" in s and "観測" not in s
    s = format_status({**base, "live_stat": {**stat, "active_observed": 180}}, now=1030)
    assert "観測 3 分" in s
    assert "推定アクティブ人数" not in format_status({**base, "live_stat": stat}, now=5000)  # 古い値は出さない
