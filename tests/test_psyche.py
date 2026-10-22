from showroom_keeper import psyche

N = psyche.MONTHS
MONTHS = [f"{2024 + (k // 12)}-{k % 12 + 1:02d}" for k in range(N)]


def _lines(rooms, fans=None, events=None, active=None, fire=100_000, career=24.0):
    total = [sum(v[i] for v in rooms.values()) for i in range(N)]
    return {"months": MONTHS, "total": total, "rooms": rooms, "fans": fans or {},
            "active": active or {r: [True] * N for r in rooms}, "season": [1.0] * N,
            "events": events or [{"room_id": r, "point": v[i], "rank": 5, "i": i, "battle": 0, "reached": 0, "judged": 0}
                                 for i in range(N) for r, v in rooms.items() if v[i]],
            "fire": fire, "career": career}


def _types(out):
    return [t["type"] for t in out["timeline"]]


def test_steady_gifter_is_stable():
    out = psyche.judge(_lines({1: [10_000] * N}, fans={1: [5] * N}))
    assert out["phase"] == "安定" and out["timeline"] == []


def test_wallet_shrinks_but_heart_stays():
    money = [10_000] * 12 + [int(10_000 * (1 - 0.07 * k)) for k in range(12)]
    out = psyche.judge(_lines({1: money}, fans={1: [5] * N}))
    assert "財布が細った" in _types(out) and out["phase"] == "冷却中"


def test_heat_cools_when_attention_falls_too():
    money = [10_000] * 12 + [int(10_000 * (1 - 0.07 * k)) for k in range(12)]
    fan = [5] * 12 + [max(1, 5 - k // 2) for k in range(12)]
    assert "熱が冷めた" in _types(psyche.judge(_lines({1: money}, fans={1: fan})))


def test_quarrel_one_room_stops_money_moves():
    a = [10_000] * 18 + [0] * 6
    b = [500] * 18 + [9_000] * 6
    out = psyche.judge(_lines({1: a, 2: b}, fans={1: [5] * 18 + [0] * 6, 2: [2] * N}))
    t = [x for x in out["timeline"] if x["type"] == "喧嘩して離れた"]
    assert t and t[0]["room_id"] == 1 and t[0]["to_room"] == 2 and t[0]["conf"] == "高"


def test_room_on_break_is_not_a_quarrel():
    a = [10_000] * 18 + [0] * 6
    b = [500] * 18 + [9_000] * 6
    act = {1: [True] * 18 + [False] * 6, 2: [True] * N}
    assert "喧嘩して離れた" not in _types(psyche.judge(_lines({1: a, 2: b}, active=act)))


def test_sudden_expense_recovers():
    money = [10_000] * 12 + [1_000, 800] + [10_000] * 10
    out = psyche.judge(_lines({1: money}, fans={1: [5] * N}))
    assert "急な出費" in _types(out)


def test_saver_goes_quiet_then_bursts():
    money = [10_000] * 7 + [0, 0, 30_000] + [10_000] * 5 + [0, 0, 0, 35_000] + [10_000] * 3 + [0, 0]
    out = psyche.judge(_lines({1: money}, fans={1: [5] * N}))
    assert _types(out).count("貯めて一気") == 2 and out["traits"]["saver"] == 2
    assert out["phase"] == "潜伏の疑い" and out["outlook"]["timing"] == "最後にまとめて出す型"
    assert out["notes"]


def test_sudden_crush_on_a_new_room():
    a = [10_000] * N
    b = [0] * 20 + [40_000] * 4
    out = psyche.judge(_lines({1: a, 2: b}, fans={1: [5] * N, 2: [0] * 20 + [8] * 4}))
    t = [x for x in out["timeline"] if x["type"] == "急にハマった"]
    assert t and t[0]["room_id"] == 2 and out["phase"] == "熱中"


def test_chasing_losses_counts_toward_addiction():
    ev = []
    for i in range(N):
        lost = i % 2 == 0
        ev.append({"room_id": 1, "point": 10_000 if lost else 20_000, "rank": 3, "i": i, "battle": 1,
                   "reached": 0 if lost else 1, "judged": 1})
    out = psyche.judge(_lines({1: [e["point"] for e in ev]}, events=ev))
    signs = {x["sign"] for x in out["traits"]["signs"]}
    assert "負けの取り返し" in signs and out["traits"]["addiction"] >= 25


def test_near_the_wall_lowers_confidence():
    a = [10_000] * 18 + [0] * 6
    b = [500] * 18 + [9_000] * 6
    lines = _lines({1: a, 2: b}, fans={1: [5] * 18 + [0] * 6, 2: [2] * N})
    for e in lines["events"]:
        e["rank"] = 90
    out = psyche.judge(lines)
    assert out["warnings"] and all(t["conf"] != "高" for t in out["timeline"])


def test_too_little_history_is_none():
    assert psyche.judge(_lines({1: [0] * 22 + [5_000, 5_000]})) is None


def test_gradual_move_to_another_room():
    a = [10_000] * 12 + [int(10_000 * (1 - 0.08 * k)) for k in range(12)]
    b = [1_000] * 12 + [1_000 + 800 * k for k in range(12)]
    out = psyche.judge(_lines({1: a, 2: b}, fans={1: [5] * N, 2: [2] * 12 + [5] * 12}))
    t = [x for x in out["timeline"] if x["type"] == "推しが移った"]
    assert t and (t[0]["room_id"], t[0]["to_room"]) == (1, 2)
    assert "冷めかけ" not in _types(out)


def test_burnout_after_an_all_in_month():
    out = psyche.judge(_lines({1: [10_000] * 15 + [40_000] + [1_000] * 8}, fans={1: [5] * 16 + [1] * 8}))
    assert "燃え尽き" in _types(out) and out["phase"] == "燃え尽きのあと" and out["traits"]["burnout"] == 1
    assert "予算を超える" in {x["sign"] for x in out["traits"]["signs"]}


def test_cooling_on_one_room_while_still_coming():
    out = psyche.judge(_lines({1: [10_000] * 20 + [4_000] * 4, 2: [5_000] * N}, fans={1: [5] * N, 2: [3] * N}))
    assert "冷めかけ" in _types(out)


def test_paying_more_while_caring_less():
    money = [5_000] * 12 + [5_000 + 1_000 * k for k in range(12)]
    fan = [8] * 12 + [max(1, 8 - k) for k in range(12)]
    out = psyche.judge(_lines({1: money}, fans={1: fan}))
    assert "楽しくないのに出す" in _types(out) and "楽しくないのに出す" in {x["sign"] for x in out["traits"]["signs"]}
