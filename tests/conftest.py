import pytest

from showroom_keeper import keeper

REAL_GIFT_UPDATES = keeper.Keeper._gift_updates
REAL_RESEARCH_UPDATES = keeper.Keeper._research_updates


@pytest.fixture(autouse=True)
def no_gift_updates(monkeypatch):
    """keeper を動かすテストで、起動時のギフトの一覧の取得と情報収集 (SHOWROOM への問い合わせ) をしない。"""
    async def idle(self):
        return None

    monkeypatch.setattr(keeper.Keeper, "_gift_updates", idle)
    monkeypatch.setattr(keeper.Keeper, "_research_updates", idle)
    monkeypatch.setattr(keeper.Keeper, "_backup_updates", idle)
    # 管理画面のテストで、GitHub に最新の版を問い合わせない
    import time

    from showroom_keeper import onlives, version

    monkeypatch.setitem(version._latest, "at", time.time())
    onlives.clear()  # 配信中の一覧の使い回しを、前のテストから持ち込まない
    from showroom_keeper import memory

    # テストを動かす機械のメモリの空きで、重い集計の後回しが変わらないようにする (後回しは、それを確かめるテストで決める)
    monkeypatch.setattr(memory, "free_mb", lambda: None)
    from showroom_keeper import catchup

    # ページを開く時の書き足しは裏の仕事に頼むが、テストではその場で足し終えてから確かめる (裏の仕事は test_catchup で確かめる)
    monkeypatch.setattr(catchup, "WAIT", True)
