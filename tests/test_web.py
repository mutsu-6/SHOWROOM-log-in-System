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
    assert data["settings"] == {"rooms": ["a", "b"], "record_rooms": [], "events": []}


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
    monkeypatch.setattr(listing, "gift_catalog", lambda state_dir: {1: {"gift_name": "星", "point": 1, "free": True}})
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
