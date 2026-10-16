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
