from showroom_keeper import event_analysis as ea
from showroom_keeper.research_store import Store

DAY = 86400
NOW = 1_800_000_000.0


def test_series_key_drops_the_numbers():
    assert ea.series_key("華金ハッピーナイト 〜木曜0時からの48時間〜Vol.54") == ea.series_key("華金ハッピーナイト 〜木曜0時からの48時間〜Vol.53")
    assert ea.series_key("やってはいけないことができる！『REEAST ROOM』イメージガール決定戦 第12弾") == \
        ea.series_key("やってはいけないことができる！『REEAST ROOM』イメージガール決定戦 第11弾")
    assert ea.series_key("【10月第2週】みんなで祝おう！誕生日パーティー♪") == ea.series_key("【11月第1週】みんなで祝おう！誕生日パーティー♪")
    assert ea.series_key("サイトモデル&SeAによるアバター制作Vol.22") != ea.series_key("日本酒の魅力を発信！")
    assert ea.series_key("vol.3") is None


def test_needed_points_from_past_events_and_projection(tmp_path):
    with Store(tmp_path) as s:
        for i, eid in enumerate((1, 2, 3)):  # 前の回 3 つ
            start, end = NOW - (30 - i * 7) * DAY, NOW - (23 - i * 7) * DAY
            s.upsert_events([{"event_id": eid, "name": f"花火大会 Vol.{eid}", "start": start, "end": end}])
            s.set_event_rooms(eid, [{"room_id": 10 + r, "point": (1000 + 100 * i) * (4 - r), "rank": r} for r in (1, 2, 3)], end + 1)
            for r in (1, 2, 3):  # 半分が過ぎた時に、最後の 4 分の 1
                s.add_progress(eid, [{"room_id": 10 + r, "point": (1000 + 100 * i) * (4 - r) // 4}], start + (end - start) / 2)
        s.upsert_events([{"event_id": 9, "name": "花火大会 Vol.9", "start": NOW - 3 * DAY, "end": NOW + 3 * DAY}])
        s.set_event_rooms(9, [{"room_id": 50, "point": 900, "rank": None}, {"room_id": 51, "point": 500, "rank": None}], NOW)
        s.upsert_events([{"event_id": 20, "name": "ちがうイベント", "start": NOW - 9 * DAY, "end": NOW - 8 * DAY}])
        n = ea.needed_points(s, 9, NOW)
    assert n["similar"] == 3
    first = next(r for r in n["ranks"] if r["rank"] == 1)
    assert first == {"rank": 1, "events": 3, "min": 3000, "median": 3300, "max": 3600}
    cur = {r["rank"]: r for r in n["current"]["ranks"]}
    assert cur[1]["now"] == 900 and cur[1]["growth"] == 4 and cur[1]["projected"] == 3600
    assert cur[2]["now"] == 500 and cur[3]["now"] is None


def test_strength_and_event_choice(tmp_path):
    with Store(tmp_path) as s:
        s.upsert_rooms([{"room_id": r, "key": f"k{r}", "name": f"ルーム{r}"} for r in (1, 2, 3, 4)], "event", NOW)
        # 前のイベント (7 日間): ルーム 1 は 7000 pt (1 日 1000)、2 は 3500、3 は 700。4 (応援ルーム) は 2 日間で 4000 (1 日 2000)
        s.upsert_events([{"event_id": 1, "name": "前", "start": NOW - 20 * DAY, "end": NOW - 13 * DAY},
                         {"event_id": 2, "name": "短い", "start": NOW - 12 * DAY, "end": NOW - 10 * DAY}])
        s.set_event_rooms(1, [{"room_id": 1, "point": 7000, "rank": 1}, {"room_id": 2, "point": 3500, "rank": 2},
                              {"room_id": 3, "point": 700, "rank": 3}], NOW - 13 * DAY + 1)
        s.set_event_rooms(2, [{"room_id": 4, "point": 4000, "rank": 1}], NOW - 10 * DAY + 1)
        s.upsert_events([{"event_id": 9, "name": "これから", "start": NOW + DAY, "end": NOW + 8 * DAY}])
        s.set_event_rooms(9, [{"room_id": r, "point": 0, "rank": None} for r in (1, 2, 3)], NOW)
        s.upsert_event_info([{"event_id": 9, "reward_rank": 1}], NOW)
        st = ea.event_strength(s, 9, NOW, [4, 3])
        assert [r["room_id"] for r in st["rooms"]] == [1, 2, 3] and st["rooms"][0]["per_day"] == 1000
        four = st["focus"][0]
        assert four["place"] == 1 and four["of"] == 4 and not four["joined"]  # 出たら 1 位くらい
        assert st["focus"][1]["place"] == 3 and st["focus"][1]["joined"]
        ch = ea.event_choice(s, [4], NOW)
    assert [e["event_id"] for e in ch["events"]] == [9] and ch["events"][0]["focus"][0]["place"] == 1
    assert ch["events"][0]["reward_rank"] == 1
