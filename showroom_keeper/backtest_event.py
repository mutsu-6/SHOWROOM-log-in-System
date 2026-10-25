"""イベントごとのルームの最終ポイントを、何も考えない予測 (B0) で当てられるかを数える (読むだけ。写しで動かす)。

Codex の設計 (docs/報告/Codexの作業_2026-10-25.md 15 章) の最初の比較。式 (地力など) をくらべる前の物差しにする。

- 1 件 = ルーム × イベント。予測の時点 T は、イベントが始まる 24 時間前
- 正解は保存済みのルームのポイント (room_events.point)。次のすべてに当てはまる時だけ「候補」として使う。外れた回は理由ごとに数え、0 にはしない。
  候補でも、検証済みの最終 pt ではない (参加ルームのページを最後まで読めたか (途中の 404 でも読めた分を保存する)・そのルームが最後の一覧に
  いたか (一覧から消えたルームの行は残る)・最終値か・pt の単位や倍率は、記録からは確かめられない)
  - そのイベントの参加ルームを、正式な結果のあと (次の日の 13:00 ごろ、research.CONFIRMED_SQL) に読み直してある
  - point が NULL ではない
  - 参加ルームが、読むページの上限 (sweep.EVENT_ROOM_PAGES) で切れていない
- B0a = 同じルームで T より前に終わった直前のイベントの最終ポイント。B0b = 直近 3 回の中央値 (3 回に満たなければ「履歴不足」)。
  履歴にも、正解と同じ条件を当てる。履歴に使うのは、正式な結果が T より前に出ていた回だけ (T にはまだ決まっていなかった結果を使わない)
- **遡及の比較**: room_events.point は読み直すたびに上書きされ、いつ分かった値かが残らない。T の時に本当にその値を
  知っていたかは確かめられないので、「今の記録で過去を振り返った比較」であって、当時の情報での予測の当たり方ではない
- 物差しは、両方の予測がある同じケースでの誤差の平均 (MAE)・中央値・偏り。予測の大きさの区分は、開発の期間の B0a の
  分け目で決め、確かめる期間にもそのまま当てる
- 開発の期間 (--dev) と確かめる期間 (--check) を分ける。区分は開発の期間だけで決める

使い方: python -m showroom_keeper.backtest_event <写しのあるフォルダ> [--dev 2026-04 --dev 2026-05 ...] [--check 2026-07 ...] [--out FILE]
写しは読み取り専用で開く (アプリの Store で開くと、古い写しでは列の足し込みを書き込むため)。サーバーでは動かさない。
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable

from . import backtest, research, sweep
from .gifter_analysis import _add

log = logging.getLogger(__name__)

VERSION = 1
FILE = "backtest-event.json"
DAY = 86400
LEAD = DAY  # 予測の時点: イベントが始まるこれだけ前
HISTORY = 3  # B0b に使う直近の回数
ROOM_CAP = sweep.EVENT_ROOM_PAGES * 30  # 参加ルームの読む上限 (1 ページ 30 件)。これに届いたイベントは切れているかもしれない
METHODS = {"b0a": "直前のイベントのポイント", "b0b": f"直近 {HISTORY} 回の中央値"}
CONFIRMED = research.CONFIRMED_SQL  # e.end から正式な結果の出る時刻

# 正解に使えるか (外れた理由)。ケースと履歴の両方に当てる
USABLE = f"""CASE WHEN e.end IS NULL THEN 'end_unknown'
                  WHEN e.rooms_at IS NULL OR e.rooms_at < {CONFIRMED} THEN 'not_read_after_result'
                  WHEN COALESCE(e.room_count, 0) >= {ROOM_CAP} THEN 'room_list_capped'
                  WHEN r.point IS NULL THEN 'point_missing'
                  ELSE 'candidate' END"""


def connect(state_dir: Path) -> sqlite3.Connection:
    """写しの research.db を読み取り専用で開く。"""
    path = Path(state_dir) / "research.db"
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _length(e: sqlite3.Row) -> str:
    """イベントの長さの区分 (日)。"""
    days = ((e["end"] or 0) - (e["start"] or 0)) / DAY
    return "3 日以下" if days <= 3 else "4〜7 日" if days <= 7 else "8 日以上"


def cases(conn: sqlite3.Connection, month: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """month (JST) に始まったイベントのルームごとのケースと、正解に使えなかった理由ごとの数。"""
    t0, t1 = backtest.month_start(month), backtest.month_start(_add(month, 1))
    out: list[dict[str, Any]] = []
    dropped: dict[str, int] = {}
    events = conn.execute("SELECT event_id, start, end, type, rooms_at, room_count FROM events INDEXED BY events_start "
                          "WHERE start >= ? AND start < ? ORDER BY start, event_id", (t0, t1)).fetchall()
    for e in events:
        at = float(e["start"]) - LEAD
        rows = conn.execute(f"""SELECT r.room_id, r.point, {USABLE} AS why FROM room_events r INDEXED BY room_events_event
                                JOIN events e ON e.event_id = r.event_id WHERE r.event_id=?""", (e["event_id"],)).fetchall()
        for r in rows:
            if r["why"] != "candidate":
                dropped[r["why"]] = dropped.get(r["why"], 0) + 1
                continue
            out.append({"event_id": e["event_id"], "room_id": r["room_id"], "at": at, "actual": float(r["point"]),
                        "length": _length(e), "type": e["type"] or "不明", **history(conn, r["room_id"], at)})
    return out, dropped


def history(conn: sqlite3.Connection, room_id: int, at: float) -> dict[str, Any]:
    """同じルームで at より前に終わり、正式な結果も at より前に出ていて (結果が決まってから使う)、正解と同じ条件を満たす直近の回から、B0a・B0b を作る。"""
    pts = [float(r["point"]) for r in conn.execute(
        f"""SELECT r.point FROM room_events r JOIN events e ON e.event_id = r.event_id
            WHERE r.room_id=? AND e.end <= ? AND {CONFIRMED} <= ? AND {USABLE} = 'candidate'
            ORDER BY e.end DESC LIMIT ?""", (room_id, at, at, HISTORY))]
    return {"b0a": pts[0] if pts else None, "b0b": median(pts) if len(pts) >= HISTORY else None, "history": len(pts)}


def _bands(xs: list[float]) -> list[float]:
    """予測の大きさの区分の分け目 (3 等分)。開発の期間だけで決める。"""
    xs = sorted(xs)
    return [xs[len(xs) // 3], xs[2 * len(xs) // 3]] if len(xs) >= 3 else []


def _band(x: float, cuts: list[float]) -> str:
    if not cuts:
        return "全部"
    return "小" if x < cuts[0] else "中" if x < cuts[1] else "大"


def errors(rows: Iterable[dict[str, Any]], key: str) -> dict[str, Any]:
    """同じケースでの誤差。偏りは (予測 − 実際) の平均で、正なら大きく出ている。"""
    rows = list(rows)
    if not rows:
        return {"n": 0}
    err = [r[key] - r["actual"] for r in rows]
    ratio = [r["actual"] / r[key] for r in rows if r[key] > 0]
    return {"n": len(rows), "mae": round(mean(abs(x) for x in err)), "median_abs": round(median(abs(x) for x in err)),
            "bias": round(mean(err)), "actual_over_pred_median": round(median(ratio), 3) if ratio else None,
            # 補助: 実際が予測の 0.5〜2 倍に入った割合 (予測が正のものだけ。MAE の代わりにはしない)
            "within_half_to_double": round(sum(1 for x in ratio if 0.5 <= x <= 2) / len(ratio), 3) if ratio else None}


def score(rows: list[dict[str, Any]], dropped: dict[str, int], cuts: list[float]) -> dict[str, Any]:
    common = [r for r in rows if all(r[k] is not None for k in METHODS)]
    out: dict[str, Any] = {
        "label_candidates": len(rows), "label_dropped": dropped,
        "b0b_short_history": sum(1 for r in rows if r["b0a"] is not None and r["b0b"] is None),  # 履歴が 3 回に満たず、同じケースから外れた数
        # 方式ごとに、正解のあるケースのうち予測を出せた割合 (履歴不足は 0 にせず、ここで数える)
        "coverage": {k: round(sum(1 for r in rows if r[k] is not None) / len(rows), 3) if rows else None for k in METHODS},
        "common": len(common),
        "methods": {k: errors(common, k) for k in METHODS},
        "by_size": {}, "by_length": {}, "by_type": {},
    }
    for name, part in (("by_size", lambda r: _band(r["b0a"], cuts)), ("by_length", lambda r: r["length"]), ("by_type", lambda r: r["type"])):
        groups: dict[str, list[dict[str, Any]]] = {}
        for r in common:
            groups.setdefault(part(r), []).append(r)
        out[name] = {g: {k: errors(xs, k) for k in METHODS} for g, xs in sorted(groups.items())}
    return out


def run(state_dir: Path, dev: list[str], check: list[str]) -> dict[str, Any]:
    started = time.time()
    conn = connect(state_dir)
    try:
        got = {m: cases(conn, m) for m in dev + check}
    finally:
        conn.close()
    dev_rows = [r for m in dev for r in got[m][0]]
    cuts = _bands([r["b0a"] for r in dev_rows if r["b0a"] is not None and r["b0b"] is not None])
    out: dict[str, Any] = {"version": VERSION, "at": time.time(), "kind": "遡及の参考比較 (今の記録で過去を振り返った。当時の情報での予測ではない)",
                           "label": "保存済みの room_events.point (候補の条件を満たすもの)。ページを読み終えたか・そのルームが最後の一覧にいたか・最終値か・pt の単位は確かめていないので、検証済みの最終 pt ではない",
                           "dev": dev, "check": check, "size_cuts_from_dev": cuts, "lead_hours": LEAD / 3600, "periods": {}}
    for label, months in (("dev", dev), ("check", check)):
        rows = [r for m in months for r in got[m][0]]
        dropped: dict[str, int] = {}
        for m in months:
            for k, v in got[m][1].items():
                dropped[k] = dropped.get(k, 0) + v
        out["periods"][label] = {"all": score(rows, dropped, cuts),
                                 "months": {m: score(got[m][0], got[m][1], cuts) for m in months}}
    out["seconds"] = round(time.time() - started)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="ルームの最終ポイントを、何も考えない予測でどれだけ当てられるか (写しで動かす。読むだけ)")
    p.add_argument("state_dir")
    p.add_argument("--dev", action="append", help="区分を決める開発の期間 (イベントの始まった月。既定 2026-04〜06)")
    p.add_argument("--check", action="append", help="確かめる期間 (既定 2026-07・08)")
    p.add_argument("--out", default=None)
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    out = run(Path(a.state_dir), a.dev or ["2026-04", "2026-05", "2026-06"], a.check or ["2026-07", "2026-08"])
    path = Path(a.out) if a.out else Path(a.state_dir) / FILE
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"結果: {path} ({out['seconds']} 秒)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
