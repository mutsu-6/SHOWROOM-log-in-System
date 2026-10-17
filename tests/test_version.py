import time

from showroom_keeper import version


def test_version_info(tmp_path, monkeypatch):
    path = tmp_path / "VERSION"
    monkeypatch.setitem(version._latest, "at", 0.0)
    monkeypatch.setitem(version._latest, "data", None)
    latest = {"commit": "abcdef123456", "date": "2026-10-17T03:00:00Z", "message": "新しい変更"}
    # VERSION が無ければ、動いている版は分からない
    r = version.info(fetch=lambda: latest, wait=True, path=path)
    assert r["current"] is None and r["latest"] == latest and r["outdated"] is None
    path.write_text("1234567aaaa 2026-10-16 12:00:00 +0900\n", encoding="utf-8")
    assert version.info(path=path)["outdated"] is True  # GitHub の方が新しい
    path.write_text("abcdef1 2026-10-17\n", encoding="utf-8")
    assert version.info(path=path)["outdated"] is False
    assert version.current(path) == {"commit": "abcdef1", "date": "2026-10-17"}


def test_version_check_failure_is_quiet(tmp_path, monkeypatch):
    monkeypatch.setitem(version._latest, "at", 0.0)
    monkeypatch.setitem(version._latest, "data", None)

    def boom():
        raise OSError("network down")

    r = version.info(fetch=boom, wait=True, path=tmp_path / "VERSION")
    assert r["latest"] is None and r["outdated"] is None
    # 失敗したら、しばらくしてからもう一度試す (毎回は問い合わせない)
    assert time.time() - version._latest["at"] < version.CHECK_EVERY
