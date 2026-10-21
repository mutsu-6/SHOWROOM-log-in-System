"""記録のファイル (ギフト・来場) の新しい分を表に足す仕事と、上の検索欄の索引の足し分を、裏で 1 本だけ行う。

ページを開く時は書き足さず、前に足した分で返して、ここに頼む (soon)。
前は、ギフターのページ・お知らせ・話題・検索・ルームのページが、開くたびに書き足していた。書き込みは 1 本ずつなので、
収集の書き込みのうしろに並んで待ち、管理画面の要求が 11 本同時に動いて、ディスク待ちが 66〜69% になっていた
(2026-10-20 に Cursor が py-spy で見た)。ふだんは収集の合間 (10 分ごと) にも足している。
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from . import memory

log = logging.getLogger(__name__)

GAP = 60  # 前に頼まれてから、次に足すまで空ける秒数 (何枚もページを開いても、続けて足さない)
WAIT = False  # True なら、頼まれたその場で足し終えてから戻る (テストで、足したあとの値を確かめるため)

_lock = threading.Lock()
_running = False
_last: dict[str, float] = {}


def soon(state_dir: Path) -> bool:
    """裏で新しい分を足す (動いている時・GAP 秒以内に足した時・自己点検が後回しにしている時は何もしない)。始めたら True。"""
    global _running
    key = str(Path(state_dir))
    if WAIT:
        _run(Path(state_dir), mark=False)
        return True
    with _lock:
        if _running or time.time() - _last.get(key, 0.0) < GAP or memory.holding():
            return False  # 後回しの間は、視聴を先にする (収集の合間にも足すので、10 分ほどで追いつく)
        _running = True
        _last[key] = time.time()
    threading.Thread(target=_run, args=(Path(state_dir),), name="catch-up", daemon=True).start()
    return True


def _run(state_dir: Path, mark: bool = True) -> None:
    global _running
    from . import explore, listing, search_index
    from .research_store import Store

    try:
        with Store(state_dir) as s:
            explore.ensure_room_totals(s)
            explore.ensure_gift_event_indexes(s, memory.holding)
            files = listing._record_files(state_dir)
            try:
                explore.repair_doubled_gifts(state_dir, s, files, memory.holding)  # 1 回だけ (終われば何もしない)
            except Exception as e:  # noqa: BLE001 - 直せなくても、新しい分は足す
                log.debug("二重に足した所を直せませんでした: %s", e)
            for key in files:
                for fn in (explore.update_gift_senders, explore.update_gift_daily):  # 送り主の表は、ギフト 1 件ずつの表と一緒に足す
                    try:
                        fn(state_dir, s, key, files)
                    except Exception as e:  # noqa: BLE001 - 1 つのルームの失敗で、ほかを止めない
                        log.debug("%s の新しいギフトを足せませんでした: %s", key, e)
            try:
                explore.update_all_live_visits(state_dir, s)
                search_index.refresh(s)
            except Exception as e:  # noqa: BLE001 - 足せなくても、次に頼まれた時にまた足す
                log.debug("来場・検索の索引を足せませんでした: %s", e)
    except Exception as e:  # noqa: BLE001
        log.debug("新しい分を足せませんでした: %s", e)
    finally:
        if mark:
            with _lock:
                _running = False
