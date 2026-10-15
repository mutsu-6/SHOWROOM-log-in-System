from showroom_keeper.gifts import from_api_log, load_recorded, merge, record_gift, summarize

CATALOG = {1: {"gift_name": "星", "point": 1}, 100: {"gift_name": "花火", "point": 100}}


def test_record_and_load(tmp_path):
    record_gift(tmp_path, "room", 5, {"t": 2, "u": 7, "ac": "Aさん", "g": 100, "n": 2, "created_at": 1000})
    record_gift(tmp_path, "room", 5, {"t": 2, "u": 8, "ac": "Bさん", "g": 1, "n": 10, "created_at": 1001})
    got = load_recorded(tmp_path, "room")
    assert [(g["name"], g["gift_id"], g["num"], g["live_id"]) for g in got] == [("Aさん", 100, 2, 5), ("Bさん", 1, 10, 5)]


def test_merge_removes_duplicates_between_record_and_api():
    recorded = [{"at": 1000, "user_id": 7, "name": "A", "gift_id": 100, "num": 2}]
    api = from_api_log([
        {"created_at": 1001, "user_id": 7, "name": "A", "gift_id": 100, "num": 2},  # 同じギフト (1 秒差)
        {"created_at": 900, "user_id": 9, "name": "C", "gift_id": 100, "num": 1},   # 記録開始前のもの
    ])
    merged = merge(recorded, api)
    assert [(g["user_id"], g["at"]) for g in merged] == [(9, 900), (7, 1000)]


def test_summarize_ranks_by_points():
    gifts = [
        {"at": 1, "user_id": 7, "name": "A", "gift_id": 1, "num": 50},   # 50 pt
        {"at": 2, "user_id": 8, "name": "B", "gift_id": 100, "num": 1},  # 100 pt
        {"at": 3, "user_id": 7, "name": "A", "gift_id": 1, "num": 10},   # A 合計 60 pt
        {"at": 4, "user_id": 9, "name": "C", "gift_id": 999, "num": 1},  # 不明ギフト
    ]
    ranking = summarize(gifts, CATALOG)
    assert [(u["name"], u["points"], u["count"], u["times"]) for u in ranking] == [
        ("B", 100, 1, 1), ("A", 60, 60, 2), ("C", 0, 1, 1)]
    assert ranking[2]["items"] == {"ギフト999": 1}


def test_match_user_by_id_and_name_including_renames():
    from showroom_keeper.gifts import match_user

    gifts = [
        {"at": 1, "user_id": 7, "name": "たろう", "gift_id": 1, "num": 1},
        {"at": 2, "user_id": 7, "name": "タロウ改", "gift_id": 1, "num": 1},  # 名前を変えた
        {"at": 3, "user_id": 8, "name": "はなこ", "gift_id": 1, "num": 1},
    ]
    assert [g["at"] for g in match_user(gifts, "7")] == [1, 2]
    assert [g["at"] for g in match_user(gifts, "たろう")] == [1, 2]  # 名前で見つけた人の改名後も含む
    assert match_user(gifts, "いない人") == []
