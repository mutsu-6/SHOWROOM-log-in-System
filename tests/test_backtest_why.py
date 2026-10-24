from showroom_keeper import backtest_why
from showroom_keeper.research_store import Store

from tests.test_backtest import MONTHS, _fill


def test_why_reads_only(tmp_path):
    with Store(tmp_path) as s:
        _fill(s)
        n = s.scalar("SELECT COUNT(*) FROM contributions")
    out = backtest_why.run(tmp_path, MONTHS, None)
    pt = out["points"][0]
    assert {"gifters", "rooms", "season"} <= set(pt)
    with Store(tmp_path) as s:  # 写しは書き換えない
        assert s.scalar("SELECT COUNT(*) FROM contributions") == n
        assert s.scalar("SELECT COUNT(*) FROM gifter_status") == 0 and s.scalar("SELECT COUNT(*) FROM event_rounds") == 0
