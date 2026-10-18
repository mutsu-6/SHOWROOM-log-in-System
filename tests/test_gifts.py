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


def test_summarize_splits_paid_and_free_points():
    catalog = {1: {"gift_name": "星", "point": 1, "free": True}, 100: {"gift_name": "花火", "point": 100, "free": False}}
    gifts = [
        {"at": 1, "user_id": 7, "name": "A", "gift_id": 1, "num": 50},
        {"at": 2, "user_id": 7, "name": "A", "gift_id": 100, "num": 2},
        {"at": 3, "user_id": 7, "name": "A", "gift_id": 999, "num": 1},  # 不明なギフトは有料に含めない
    ]
    (u,) = summarize(gifts, catalog)
    assert (u["points"], u["paid_points"], u["free_points"]) == (250, 200, 50)


def _contrib_fetch(calls, fail=()):
    def fetch(path):
        room_id = int(path.rsplit("=", 1)[1])
        calls.append(room_id)
        if room_id in fail:
            raise OSError("down")
        return {"ranking": [{"rank": 1, "user_id": 7, "name": "A", "point": room_id * 10},
                            {"rank": 2, "user_id": room_id, "name": f"U{room_id}", "point": 5}]}
    return fetch


def test_fetch_contributions_collects_every_room_and_caches(tmp_path):
    from showroom_keeper.gifts import fetch_contributions, summarize_official

    rooms = [{"room_id": 1, "room_name": "R1"}, {"room_id": 2, "room_name": "R2"}, {"room_name": "ID なし"}]
    calls = []
    rows, failed = fetch_contributions(tmp_path, 99, rooms, final=False, fetch=_contrib_fetch(calls), now=1000)
    assert failed == 0 and sorted(calls) == [1, 2] and len(rows) == 4
    top = summarize_official(rows)[0]
    assert (top["user_id"], top["points"], top["rooms"]) == (7, 30, [("R2", 20), ("R1", 10)])

    # 開催中は 10 分間だけ使い回し、過ぎたら取り直す
    fetch_contributions(tmp_path, 99, rooms, final=False, fetch=_contrib_fetch(calls), now=1500)
    assert len(calls) == 2
    fetch_contributions(tmp_path, 99, rooms, final=True, fetch=_contrib_fetch(calls), now=2000)
    assert len(calls) == 4
    # 終了済み (確定値) として保存したものは期限なしで使う
    fetch_contributions(tmp_path, 99, rooms, final=True, fetch=_contrib_fetch(calls), now=10 ** 9)
    assert len(calls) == 4


def test_fetch_contributions_does_not_cache_partial_results(tmp_path):
    from showroom_keeper.gifts import contribution_cache, fetch_contributions

    rooms = [{"room_id": 1, "room_name": "R1"}, {"room_id": 2, "room_name": "R2"}]
    rows, failed = fetch_contributions(tmp_path, 5, rooms, final=True, fetch=_contrib_fetch([], fail={2}), now=1)
    assert failed == 1 and {r["room"] for r in rows} == {"R1"}
    assert not contribution_cache(tmp_path, 5).exists()


def test_official_ranking_uses_collected_rankings_first(tmp_path, monkeypatch):
    from showroom_keeper import research
    from showroom_keeper.gifts import fetch_contributions
    from showroom_keeper.research_store import Store

    with Store(tmp_path) as s:  # 全ルームの収集が、ルーム 1 の分はもう取ってある
        s.set_contributions(5, 1, [{"user_id": 7, "name": "集めた人", "point": 300, "rank": 1}], 1, final=True)
    asked = []
    monkeypatch.setattr(research.THROTTLE, "call", lambda get, path: asked.append(path) or {"ranking": [
        {"rank": 1, "user_id": 8, "name": "聞いた人", "point": 50}]})
    rows, failed = fetch_contributions(tmp_path, 5, [{"room_id": 1, "room_name": "A"}, {"room_id": 2, "room_name": "B"}],
                                       final=True, now=1)
    assert failed == 0 and {(r["room"], r["user_id"], r["name"]) for r in rows} == {("A", 7, "集めた人"), ("B", 8, "聞いた人")}
    assert asked == ["/api/event/contribution_ranking?event_id=5&room_id=2"]  # 取ってない分だけ、決まりを通して聞く
