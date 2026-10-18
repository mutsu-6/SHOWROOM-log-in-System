import base64
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from showroom_keeper.api import RoomStatus
from showroom_keeper.config import Config, apply_settings, settings_path
from showroom_keeper.web import StatusCache, host_is_loopback, post_comment, start_admin


class FakeWatcher:
    def __init__(self, key, live):
        self.key = key
        self.status = RoomStatus(key, 1, f"{key} の部屋", live, live_id=5 if live else None)
        self.blocked = False
        self.watched_seconds = 600
        self.reload_count = 1
        self.stall_count = 0
        self.can_comment = live

    def _page_alive(self):
        return True


class FakeKeeper:
    def __init__(self, cfg):
        self.cfg = cfg
        self.watchers = [FakeWatcher(k, i == 0) for i, k in enumerate(cfg.rooms)]
        self.recorder = None
        self.started_at = time.time() - 120
        self.browser_state = "動作中"
        self.reloads = 0

    def request_reload(self):
        self.reloads += 1


@pytest.fixture
def admin(tmp_path):
    cfg = Config(rooms=["a", "b"], state_file=tmp_path / "s.json", web_port=0)
    keeper = FakeKeeper(cfg)
    cache = StatusCache(fetcher=lambda key: {"key": key, "name": f"{key} の部屋", "is_live": key == "a",
                                             "started_at": None, "excitement": 10, "commenters": 2,
                                             "commenters_capped": False})
    server = start_admin(keeper, cache)
    yield keeper, f"http://127.0.0.1:{server.port}"
    server.shutdown()


def call(url, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


POST = {"Content-Type": "application/json", "X-Showroom-Keeper": "1"}


def test_page_and_overview(admin):
    keeper, base = admin
    code, html = call(base + "/")
    assert code == 200 and "SHOWROOM keeper" in html
    code, body = call(base + "/api/overview")
    data = json.loads(body)
    assert code == 200 and [r["key"] for r in data["rooms"]] == ["a", "b"]
    a, b = data["rooms"]
    assert a["watcher"]["state"] == "watching" and a["watcher"]["can_comment"] is True
    assert b["watcher"]["state"] == "waiting" and a["excitement"] == 10
    assert data["settings"] == {"rooms": ["a", "b"], "record_rooms": [], "events": [], "enter_rooms": [], "no_visit_rooms": [], "support_rooms": [], "research_rooms": []}


def test_rejects_other_hosts_and_cross_site_posts(admin):
    keeper, base = admin
    # DNS リバインディング: 他のドメイン名で localhost に届いた要求は断る
    code, _ = call(base + "/api/overview", headers={"Host": "evil.example:8080"})
    assert code == 403
    # 他のサイトのフォームからは独自ヘッダを付けられない
    code, _ = call(base + "/api/settings", {"rooms": ["x"]}, {"Content-Type": "application/json"})
    assert code == 403 and keeper.reloads == 0


def test_save_settings_and_request_reload(admin, tmp_path):
    keeper, base = admin
    code, body = call(base + "/api/settings", {
        "rooms": ["https://www.showroom-live.com/r/new_room", "new_room"],
        "record_rooms": ["rec"], "events": ["https://www.showroom-live.com/event/ev1"],
    }, POST)
    assert code == 200 and json.loads(body)["settings"]["rooms"] == ["new_room"]
    assert keeper.reloads == 1
    cfg = Config(state_file=tmp_path / "s.json")
    assert apply_settings(cfg) and (cfg.rooms, cfg.record_rooms, cfg.events) == (["new_room"], ["rec"], ["ev1"])
    assert cfg.enter_rooms == []
    code, body = call(base + "/api/settings", {"rooms": ["not a room!"]}, POST)
    assert code == 400 and keeper.reloads == 1


def test_password_allows_other_hosts(tmp_path):
    cfg = Config(rooms=[], state_file=tmp_path / "s.json", web_port=0, web_password="秘密")
    server = start_admin(FakeKeeper(cfg), StatusCache(fetcher=lambda k: {"key": k}))
    base = f"http://127.0.0.1:{server.port}"
    try:
        assert call(base + "/api/overview")[0] == 401
        bad = base64.b64encode("u:違う".encode()).decode()
        assert call(base + "/api/overview", headers={"Authorization": f"Basic {bad}"})[0] == 401
        good = base64.b64encode("u:秘密".encode()).decode()
        code, _ = call(base + "/api/overview", headers={"Authorization": f"Basic {good}", "Host": "192.0.2.1:8080"})
        assert code == 200
    finally:
        server.shutdown()


def test_post_comment_waits_for_keeper(tmp_path):
    from showroom_keeper.comments import queue_dir

    cfg = Config(rooms=["a"], state_file=tmp_path / "s.json")
    qdir = queue_dir(cfg)

    def keeper_side():  # 常駐中の keeper が依頼を拾って結果を書く代わり
        for _ in range(50):
            reqs = list(qdir.glob("*.req.json")) if qdir.exists() else []
            if reqs:
                req = reqs[0]
                data = json.loads(req.read_text(encoding="utf-8"))
                req.unlink()
                rid = req.name[: -len(".req.json")]
                (qdir / f"{rid}.result.json").write_text(json.dumps({"ok": True, "message": f"投稿しました: {data['text']}"}),
                                                         encoding="utf-8")
                return
            time.sleep(0.05)

    t = threading.Thread(target=keeper_side)
    t.start()
    result = post_comment(FakeKeeper(cfg), "  こんにちは  ", "a", timeout=5)
    t.join()
    assert result == {"ok": True, "message": "投稿しました: こんにちは"}
    assert post_comment(FakeKeeper(cfg), "あ" * 51, None)["ok"] is False
    assert post_comment(FakeKeeper(cfg), "誰も受け取らない", None, timeout=0.3)["ok"] is False
    assert not list(qdir.glob("*.req.json"))  # 受け取られなかった依頼は取り消す


def test_host_is_loopback():
    assert host_is_loopback("localhost:8080") and host_is_loopback("127.0.0.1") and host_is_loopback("[::1]:8080")
    assert not host_is_loopback("example.com") and not host_is_loopback(None) and not host_is_loopback("192.168.0.2:8080")


async def test_keeper_applies_saved_settings_without_restart(monkeypatch, tmp_path):
    import asyncio

    from showroom_keeper import keeper as kp
    from showroom_keeper.config import save_settings

    seen = []

    class FakeRecorder:
        def __init__(self, cfg):
            seen.append(list(cfg.record_rooms))

        async def run(self):
            await asyncio.Event().wait()

        def active(self):
            return []

    monkeypatch.setattr("showroom_keeper.recorder.GiftRecorder", FakeRecorder)
    cfg = Config(record_rooms=["a"], state_file=tmp_path / "s.json", web_port=0)
    keeper = kp.Keeper(cfg)
    task = asyncio.ensure_future(keeper.run())
    for _ in range(50):
        if seen:
            break
        await asyncio.sleep(0.01)
    save_settings(cfg, {"record_rooms": ["b", "c"]})
    keeper.request_reload()
    for _ in range(100):
        if len(seen) == 2:
            break
        await asyncio.sleep(0.01)
    keeper.stop()
    await asyncio.wait_for(task, timeout=2)
    assert seen == [["a"], ["b", "c"]]
    assert settings_path(cfg).exists()


def test_list_endpoints(admin, tmp_path, monkeypatch):
    from showroom_keeper import listing
    from showroom_keeper.gifts import record_gift

    keeper, base = admin
    monkeypatch.setattr(listing, "_names_cache", None)
    monkeypatch.setattr(listing, "_file_cache", {})
    monkeypatch.setattr(listing, "gift_catalog", lambda state_dir, rooms=None: {1: {"gift_name": "星", "point": 1, "free": True}})
    record_gift(tmp_path, "a", 1, {"t": 2, "u": 7, "ac": "たろう", "g": 1, "n": 3, "created_at": time.time()})
    code, body = call(base + "/api/lists/rooms")
    rooms = {r["key"]: r for r in json.loads(body)["rooms"]}
    assert code == 200 and rooms["a"]["records"] == 1 and rooms["a"]["is_live"] and "watch" in rooms["a"]["tags"]
    code, body = call(base + "/api/listeners?days=7")
    assert code == 200 and json.loads(body)["listeners"][0]["points"] == 3
    code, body = call(base + "/api/listener?user=7")
    assert code == 200 and json.loads(body)["total"]["times"] == 1
    assert call(base + "/api/listener?user=abc")[0] == 400
    monkeypatch.setattr(listing, "lookup_user", lambda uid: {"user_id": int(uid), "name": "だれか"})
    code, body = call(base + "/api/lookup/user?id=12")
    assert code == 200 and json.loads(body)["user"]["name"] == "だれか"
    assert call(base + "/api/lookup/user?id=x1")[0] == 400


def test_research_endpoints(admin, monkeypatch):
    from showroom_keeper import research

    keeper, base = admin
    started = []
    monkeypatch.setattr(research.JOB, "start", lambda state_dir, keys, reason, force=False: started.append((keys, force)) or True)
    monkeypatch.setattr(research, "analyze", lambda state_dir, support, max_age=600: {
        "candidates": [], "support_arg": support, "max_age": max_age})
    keeper.cfg.support_rooms = ["sup"]
    code, body = call(base + "/api/research")
    data = json.loads(body)
    assert code == 200 and data["support_arg"] == ["sup"] and data["job"]["running"] is False and data["max_age"] == 600
    assert json.loads(call(base + "/api/research?refresh=1")[1])["max_age"] == 0  # 「集計し直す」
    code, body = call(base + "/api/research/status")  # 集めている間の進み具合 (分析はしない)
    data = json.loads(body)
    assert code == 200 and "candidates" not in data and data["sweep"] == {"enabled": False} and data["throttle"]["gap"] >= 1
    code, body = call(base + "/api/research/collect", {"force": True}, POST)
    assert code == 200 and json.loads(body)["started"] and started == [(["sup"], True)]
    # 応援ルームだけ変えた時は、視聴をやり直さず、まだ集めていないルームを集める
    code, _ = call(base + "/api/settings", {"rooms": ["a", "b"], "support_rooms": ["sup", "new"]}, POST)
    assert code == 200 and keeper.reloads == 0 and keeper.cfg.support_rooms == ["sup", "new"]
    assert started[-1] == (["sup", "new"], False)
    # 視聴のルームと一緒に変えた時も、新しい応援ルームをすぐ集める (視聴のやり直しを待たない)
    code, _ = call(base + "/api/settings", {"rooms": ["c"], "support_rooms": ["sup", "new", "newer"]}, POST)
    assert code == 200 and keeper.reloads == 1 and started[-1] == (["sup", "new", "newer"], False)
    assert call(base + "/api/research/gifter?user=x")[0] == 400


def test_explore_endpoints(admin):
    _, base = admin
    for path in ("/api/search?q=abc", "/api/room?room=abc", "/api/events?state=ended", "/api/event?event=5"):
        code, body = call(base + path)
        assert code == 200, path
    assert json.loads(call(base + "/api/room?room=abc")[1]) == {"key": "abc", "found": False}
    assert call(base + "/api/events?state=bad")[0] == 400
    assert call(base + "/api/room?room=not%20a%20room!")[0] == 400
    assert json.loads(call(base + "/api/series")[1])["series"] == []
    assert json.loads(call(base + "/api/series?id=5")[1])["found"] is False
    assert call(base + "/api/series/link", {"event_id": "5"}, POST)[0] == 400  # つなぐ先が無い
    assert call(base + "/api/series/link", {"event_id": "5", "detach": True}, POST)[0] == 400  # イベントが無い


def test_alerts_for_login_disk_and_backup(tmp_path, monkeypatch):
    import shutil
    from collections import namedtuple

    from showroom_keeper import backup
    from showroom_keeper.web import build_alerts

    now = time.time()
    assert build_alerts(tmp_path, {"logged_in": True, "checked_at": now, "check_interval": 7200}, now) == []
    assert "切れています" in build_alerts(tmp_path, {"logged_in": False, "checked_at": now}, now)[0]
    assert "途絶えて" in build_alerts(tmp_path, {"logged_in": True, "checked_at": now - 8 * 3600, "check_interval": 7200}, now)[0]
    backup._save_last(tmp_path, {"ok": False, "at": now, "reason": "ディスクの空きが足りません"})
    assert build_alerts(tmp_path, {}, now) == ["research.db のバックアップに失敗しました: ディスクの空きが足りません"]
    backup._save_last(tmp_path, {"ok": True, "at": now - 3 * 86400})
    assert "2 日以上" in build_alerts(tmp_path, {}, now)[0]
    backup._save_last(tmp_path, {"ok": True, "at": now})
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: usage(10 << 30, 9 << 30, 1 << 30))
    assert "1.0GB" in build_alerts(tmp_path, {}, now)[0]


def test_alerts_for_stalled_sweep_pauses_and_disk_forecast(tmp_path, monkeypatch):
    import shutil
    from collections import namedtuple

    from showroom_keeper import research, sweep, web

    now = time.time()
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: usage(30 << 30, 20 << 30, 10 << 30))
    monkeypatch.setattr(sweep, "state", lambda: {"enabled": True, "last_step_at": now - 2 * 3600, "doing": "ルームの分析"})
    monkeypatch.setattr(research.THROTTLE, "pause_log", [{"at": now - 60}] * 3)
    alerts = web.build_alerts(tmp_path, {}, now)
    assert any("2 時間進んでいません" in a for a in alerts)
    assert any("3 回止められました" in a for a in alerts)
    assert not any("いっぱい" in a for a in alerts)  # 7 日分たまるまでは見積もらない
    # 30 日前に空きが 40GB で、今 10GB (1 日 1GB ずつ減る) → あと 10 日
    from showroom_keeper import backup

    (tmp_path / backup.DISK_FILE).write_text(json.dumps([[now - 30 * 86400, 40 << 30]]), encoding="utf-8")
    assert backup.disk_days_left(tmp_path, 10 << 30, now) == pytest.approx(10, rel=0.01)
    monkeypatch.setattr(research.THROTTLE, "pause_log", [])
    monkeypatch.setattr(sweep, "state", lambda: {"enabled": False})
    alerts = web.build_alerts(tmp_path, {}, now)
    assert len(alerts) == 1 and "あと 10 日ほど" in alerts[0]
