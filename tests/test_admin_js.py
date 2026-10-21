"""管理画面 (admin.html) の JavaScript が、読み込めるか (同じ名前を 2 回宣言するなどの書き間違いで、画面全体が動かなくなるのを防ぐ)。

node があれば `node --check` で文法だけ確かめる (無い所では飛ばす)。
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ADMIN = Path(__file__).resolve().parent.parent / "showroom_keeper" / "admin.html"


@pytest.mark.skipif(shutil.which("node") is None, reason="node がありません")
def test_admin_script_parses(tmp_path):
    scripts = re.findall(r"<script>(.*?)</script>", ADMIN.read_text(encoding="utf-8"), re.S)
    assert scripts
    for k, js in enumerate(scripts):
        f = tmp_path / f"admin{k}.js"
        f.write_text(js, encoding="utf-8")
        r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
