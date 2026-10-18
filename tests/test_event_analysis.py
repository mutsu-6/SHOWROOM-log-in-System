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
