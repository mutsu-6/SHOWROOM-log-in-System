import time

from showroom_keeper import version


def test_version_info(tmp_path, monkeypatch):
    path = tmp_path / "VERSION"
    monkeypatch.setitem(version._latest, "at", 0.0)
    monkeypatch.setitem(version._latest, "data", None)
    monkeypatch.setitem(version._latest, "for", None)
    latest = {"commit": "abcdef123456", "date": "2026-10-17T03:00:00Z", "message": "新しい変更"}
    # VERSION が無ければ、動いている版は分からない
    r = version.info(fetch=lambda: latest, wait=True, path=path)
    assert r["current"] is None and r["latest"] == latest and r["outdated"] is None
    path.write_text("1234567aaaa 2026-10-16 12:00:00 +0900\n", encoding="utf-8")
    assert version.info(fetch=lambda: latest, wait=True, path=path)["outdated"] is True  # GitHub の方が新しい
    path.write_text("abcdef1 2026-10-17\n", encoding="utf-8")
    assert version.info(fetch=lambda: latest, wait=True, path=path)["outdated"] is False
    assert version.current(path) == {"commit": "abcdef1", "date": "2026-10-17"}


def test_version_rechecks_when_running_version_changes(tmp_path, monkeypatch):
    # 説明書だけの変更では、起動し直さずに VERSION だけが進む。覚えている GitHub の最新が古いままでも、
    # 「更新が残っています」と出さず、すぐ調べ直す
    path = tmp_path / "VERSION"
    path.write_text("aaaaaaa 2026-10-20\n", encoding="utf-8")
    monkeypatch.setitem(version._latest, "at", 0.0)
    monkeypatch.setitem(version._latest, "data", None)
    monkeypatch.setitem(version._latest, "for", None)
    monkeypatch.setitem(version._latest, "seen", None)
    calls = []

    def fetch(commit):
        def f():
            calls.append(commit)
            return {"commit": commit, "date": None, "message": ""}
        return f

    assert version.info(fetch=fetch("aaaaaaa00000"), wait=True, path=path)["outdated"] is False
    assert version.info(fetch=fetch("unused"), path=path)["outdated"] is False  # 1 時間はもう問い合わせない
    path.write_text("bbbbbbb 2026-10-21\n", encoding="utf-8")
    assert version.info(fetch=fetch("bbbbbbb00000"), wait=True, path=path)["outdated"] is False
    assert calls == ["aaaaaaa00000", "bbbbbbb00000"]

    gate = __import__("threading").Event()

    def slow():
        gate.wait(5)
        return {"commit": "ccccccc00000", "date": None, "message": ""}

    path.write_text("ccccccc 2026-10-21\n", encoding="utf-8")
    assert version.info(fetch=slow, path=path)["outdated"] is None  # 調べ直している間は決めない
    assert version.info(fetch=slow, path=path)["outdated"] is None
    gate.set()


def test_version_check_failure_is_quiet(tmp_path, monkeypatch):
    monkeypatch.setitem(version._latest, "at", 0.0)
    monkeypatch.setitem(version._latest, "data", None)
    monkeypatch.setitem(version._latest, "for", None)

    def boom():
        raise OSError("network down")

    r = version.info(fetch=boom, wait=True, path=tmp_path / "VERSION")
    assert r["latest"] is None and r["outdated"] is None
    # 失敗したら、しばらくしてからもう一度試す (毎回は問い合わせない)
    assert time.time() - version._latest["at"] < version.CHECK_EVERY
