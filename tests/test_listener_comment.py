"""リスナーのコメントを止めたとき、配信のコメント欄は増えない。"""

import json

from showroom_keeper import keeper as kp
from showroom_keeper import mission
from showroom_keeper.comments import CommentDispatcher, enqueue
from showroom_keeper.config import Config
from showroom_keeper.web import post_comment
from showroom_keeper.watcher import RoomWatcher
from showroom_keeper.api import RoomStatus


class Stream:
    """配信に出ているコメント。投稿の通信が届いたときだけ増える。"""

    def __init__(self):
        self.comments = []

    def receive(self, text):
        self.comments.append(text)


class Page:
    def __init__(self, stream):
        self.stream = stream
        self.closed = False

    def is_closed(self):
        return self.closed

    async def fill(self, sel, value, **kw):
        self.stream.receive(value)

    async def press(self, sel, key, **kw):
        pass

    async def evaluate(self, js, arg=None):
        return {"user_id": 1}

    async def goto(self, url, **kw):
        pass

    async def reload(self, **kw):
        pass

    async def close(self):
        self.closed = True


class Ctx:
    def __init__(self):
        self.routes = []
        self.scripts = []

    async def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    async def add_init_script(self, script):
        self.scripts.append(script)


class Route:
    def __init__(self, url):
        self.url = url
        self.aborted = False

    async def abort(self):
        self.aborted = True


async def test_stopped_comment_is_absent_from_the_stream(tmp_path):
    stream = Stream()
    cfg = Config(rooms=["room"], state_file=tmp_path / "s.json", listener_comment=False)
    pages = []

    async def fetch(key):
        return RoomStatus(key, 1, "配信", True, live_id=9)

    async def new_page():
        pages.append(Page(stream))
        return pages[-1]

    watcher = RoomWatcher("room", cfg, fetch, new_page)
    await watcher.step()
    ok, msg = await watcher.post_comment("載ってはいけない")
    assert ok is False and msg == "リスナーのコメントは止めています"
    assert stream.comments == []

    ctx = Ctx()
    await kp.install_listener_comment_block(ctx, cfg)
    assert any("リスナーのコメントは止めています" in s for s in ctx.scripts)
    posted = Route("https://www.showroom-live.com/api/live/post_live_comment")
    handled = False
    for pattern, handler in ctx.routes:
        if pattern.search(posted.url):
            handled = True
            await handler(posted)
    assert handled and posted.aborted and stream.comments == []

    # 送る設定のときは、ページの投稿を捨てない
    open_cfg = Config(rooms=["room"], state_file=tmp_path / "s.json", listener_comment=True)
    open_ctx = Ctx()
    await kp.install_listener_comment_block(open_ctx, open_cfg)
    assert open_ctx.routes == [] and open_ctx.scripts == []

    class Session:
        def post(self, path, form):
            stream.receive(form["comment"])
            return json.dumps({"ok": 1})

    runner = mission.MissionRunner(cfg, lambda: [], lambda: {}, session=lambda c: Session())
    sent = await runner.comment(Session(), {"live_id": 9}, "載ってはいけない")
    assert sent is False and stream.comments == []

    result = post_comment(type("K", (), {"cfg": cfg})(), "載ってはいけない", "room", timeout=0.1)
    assert result == {"ok": False, "message": "リスナーのコメントは止めています"}
    assert not (tmp_path / "comments").exists()

    class Target:
        key = "room"
        can_comment = True
        posted = []

        async def post_comment(self, text):
            self.posted.append(text)
            stream.receive(text)
            return True, "投稿しました"

    target = Target()
    clock = {"t": 1000.0}

    async def sleep(sec):
        clock["t"] += sec

    dispatcher = CommentDispatcher(tmp_path / "q", [target], clock=lambda: clock["t"], sleep=sleep,
                                   may_comment=lambda: False)
    req = enqueue(tmp_path / "q", "載ってはいけない", room="room", now=clock["t"])
    await dispatcher.process_pending()
    assert target.posted == [] and stream.comments == []
    saved = json.loads((tmp_path / "q" / f"{req}.result.json").read_text(encoding="utf-8"))
    assert saved == {"ok": False, "message": "リスナーのコメントは止めています"}
