from showroom_keeper import live_events as le


def test_parse_visits_and_fan_levels():
    assert le.parse({"t": 18, "u": 7, "m": "あいうさんが初訪問✨"}) == {"user_id": 7, "kind": "visit", "n": 1}
    assert le.parse({"t": 18, "u": 7, "m": "あいうさんが12度目の訪問✨"})["n"] == 12
    assert le.parse({"t": 18, "u": 7, "m": "あいうさんが3回目の訪問🎉"})["n"] == 3
    x = le.parse({"t": 18, "u": 8, "m": "名前のファンレベルが13にあがりました！(4ヶ月連続4回目)"})
    assert x == {"user_id": 8, "kind": "fan_level", "level": 13, "streak": 4, "times": 4}
    assert le.parse({"t": 18, "u": 8, "m": "名前のファンレベルが5にあがりました！(2ヶ月ぶり3回目)"})["gap"] == 2
    assert le.parse({"t": 18, "u": 8, "m": "名前のファンレベルが1にあがりました！(はじめて)"})["first"] is True
    assert le.parse({"t": 18, "u": 8, "m": "名前の名前のファンレベルが20にあがりました！"})["level"] == 20  # 名前に「の」があっても
    assert le.parse({"t": 18, "m": "ファンの名前が300人になりました！"}) == {"user_id": None, "kind": "fans", "n": 300}
    assert le.parse({"t": 18, "m": "知らない知らせ"})["kind"] == "other"
    assert le.parse({"t": 1001, "message": "定番ギフトランキング「花束」で日間2位になりました"})["kind"] == "notice"
    assert le.parse({"t": 1, "cm": "こんにちは"}) is None


def test_record_and_load(tmp_path):
    assert le.record(tmp_path, "room", 5, {"t": 18, "u": 7, "m": "あさんが初訪問✨", "created_at": 100})
    assert not le.record(tmp_path, "room", 5, {"t": 2, "g": 1})
    assert le.load(tmp_path, "room") == [{"at": 100, "live_id": 5, "user_id": 7, "kind": "visit", "n": 1}]
