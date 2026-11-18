"""r236 护栏：底栏实时速率 + 图库框选 + 任务「在文件夹中打开」。

这些功能有个共同特点——**错了也不会报错，只会静默不对**：
- 速率分桶错一格，柱图就整排错位，看着"有数据"其实是假的；
- 框选少吞一次 click，刚框好的 20 张会瞬间变回 1 张；
- reveal 没判远程，用户点一下"打开目录"却什么也没发生。

所以全部用静态判据 + 真跑 SQL 钉住。
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from media_resource_manager.db import Database


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "media_resource_manager/webapp/src"


def _read(rel: str) -> str:
    return (SRC / rel).read_text(encoding="utf-8")


# ------------------------------------------------------------ 速率（后端）

def _db_with_images(tmp_path) -> tuple[Database, int]:
    db = Database(tmp_path / "mrm.db")
    db.execute("INSERT INTO tasks(id, name, adapter_name, entry_urls) "
               "VALUES (1,'t','generic','[]')")
    rows = [("https://e/1", 1000, "now"), ("https://e/2", 2000, "now"),
            ("https://e/3", 3000, "now"), ("https://e/4", 4000, "-3 minutes"),
            ("https://e/5", 5000, "-1 day")]
    for url, b, off in rows:
        if off == "now":
            db.execute("INSERT INTO images(task_id, source_url, bytes, path, "
                       "collected_at) VALUES (1,?,?,?,datetime('now'))", (url, b, url))
        else:
            db.execute("INSERT INTO images(task_id, source_url, bytes, path, "
                       "collected_at) VALUES (1,?,?,?,datetime('now',?))",
                       (url, b, url, off))
    return db, 1


def test_stats_rate_and_buckets(tmp_path):
    from media_resource_manager.webui import _Handler

    db, _ = _db_with_images(tmp_path)
    stub = SimpleNamespace(db=db)
    d = _Handler._api_stats(stub)
    assert d["ok"] is True, d
    assert d["last_min"] == 3, d            # 只有"刚才"那 3 张在 60 秒内
    assert abs(d["rate_bps"] - 100.0) < 0.6, d   # 6000 字节 / 60 秒
    pm = d["per_min"]
    assert len(pm) == 10, pm                # 固定 10 格，缺格会让柱子跳位
    assert pm[-1] == 3, pm
    assert pm[6] == 1, pm                   # 3 分钟前那张落在倒数第 4 格
    assert pm[:6] == [0] * 6, pm
    # 昨日那张必须被排除：today 只算 4 张
    assert d["today"] == 4, d
    assert d["today_bytes"] == 10000, d


def test_stats_never_raises_on_empty_db(tmp_path):
    """底栏轮询是 5 秒一次 —— 这里抛异常会刷满日志，且整条底栏消失。"""
    from media_resource_manager.webui import _Handler

    db = Database(tmp_path / "mrm.db")
    d = _Handler._api_stats(SimpleNamespace(db=db))
    assert d["ok"] is True, d
    assert d["per_min"] == [0] * 10, d
    assert d["rate_bps"] == 0


def test_collected_at_index_exists(tmp_path):
    """没有索引时每次 5 秒采样都是全表扫描（上万行时肉眼可卡）。"""
    db = Database(tmp_path / "mrm.db")
    names = [r["name"] for r in db.query(
        "SELECT name FROM sqlite_master WHERE type='index'")]
    assert "idx_images_collected" in names, names


# ------------------------------------------------------ 打开目录（后端）

def _reveal(host: str, tmp_path, out_dir="", create=True):
    from media_resource_manager.repo import save_task
    from media_resource_manager.models import TaskConfig
    from media_resource_manager.webui import _Handler

    db = Database(tmp_path / "mrm.db")
    d = tmp_path / "out"
    if create:
        d.mkdir(exist_ok=True)
    tid = save_task(db, TaskConfig(id=None, name="t", adapter_name="generic",
                                   entry_urls=["https://e/1"],
                                   output_dir=str(d) if out_dir == "auto" else out_dir))
    stub = SimpleNamespace(db=db, headers={"Host": host})
    return _Handler._api_task_reveal(stub, {"id": tid}), d


def test_reveal_rejects_remote_host(tmp_path, monkeypatch):
    """远程访问时打开的是服务端机器 —— 必须说清，而不是静默成功。"""
    called = []
    monkeypatch.setattr("os.startfile", lambda p: called.append(p), raising=False)
    r, d = _reveal("192.168.1.5:8080", tmp_path, out_dir="auto")
    assert r["ok"] is False, r
    assert "远程" in r["error"], r
    assert str(d) in r["error"], r          # 路径也要给，用户能自己去开
    assert not called, "远程请求绝不能在服务端弹资源管理器"


def test_reveal_opens_local_dir(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr("os.startfile", lambda p: called.append(p), raising=False)
    r, d = _reveal("127.0.0.1:8080", tmp_path, out_dir="auto")
    assert r["ok"] is True, r
    assert r["path"] == str(d), r
    assert called == [str(d)], called


def test_reveal_reports_missing_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("os.startfile", lambda p: None, raising=False)
    r, _ = _reveal("127.0.0.1:8080", tmp_path, out_dir="auto", create=False)
    assert r["ok"] is False and "不存在" in r["error"], r


def test_reveal_without_output_dir_explains(tmp_path):
    r, _ = _reveal("127.0.0.1:8080", tmp_path, out_dir="")
    assert r["ok"] is False and "输出目录" in r["error"], r


def test_reveal_bad_id(tmp_path):
    from media_resource_manager.webui import _Handler
    stub = SimpleNamespace(db=Database(tmp_path / "mrm.db"), headers={"Host": "127.0.0.1"})
    for bad in ("abc", None, 0):
        r = _Handler._api_task_reveal(stub, {"id": bad})
        assert r["ok"] is False, (bad, r)
    r = _Handler._api_task_reveal(stub, {"id": 987654})
    assert r["ok"] is False and "不存在" in r["error"], r


# ---------------------------------------------------------- 框选（前端）

def test_gallery_supports_marquee_selection():
    g = _read("components/ImageGrid.vue")
    assert "onGridDown" in g and "onGridMove" in g and "onGridUp" in g
    assert 'class="marquee"' in g
    assert ":data-id=\"it.id\"" in g, "框选按 data-id 认图，缺了等于框不住"
    assert "e.pointerType === 'touch'" in g, "触屏不能抢手势（否则滚不动页面）"


def test_marquee_swallows_the_following_click():
    """pointerup 之后浏览器还会补一个 click，不吞掉就把框选结果重置成单张。"""
    g = _read("components/ImageGrid.vue")
    assert "suppressClick" in g
    assert "if (suppressClick) return" in g
    # 必须用 setTimeout 延后失效：同步清掉的话 click 还没来就已经恢复
    assert "setTimeout(() => { suppressClick = false }, 0)" in g


def test_marquee_ignores_tiny_drag():
    """手抖 2px 不该变成"框了个空框把选择清光"。"""
    g = _read("components/ImageGrid.vue")
    assert "Math.abs(dx) < 5" in g and "Math.abs(dy) < 5" in g


def test_marquee_uses_intersection_not_containment():
    g = _read("components/ImageGrid.vue")
    assert "r.right < box.x1" in g and "r.left > box.x2" in g


# ------------------------------------------------------ 底栏速率（前端）

def test_statusbar_shows_rate_and_spark():
    a = _read("App.vue")
    assert "api.stats()" in a
    assert "setInterval(refreshStats, 5000)" in a
    assert "function fmtRate" in a
    assert 'class="spark"' in a
    assert "document.hidden" in a, "页面在后台不该继续轮询（白耗电）"


def test_stats_timer_is_cleared_on_unmount():
    a = _read("App.vue")
    assert "clearInterval(statsTimer)" in a


def test_spark_zero_is_a_thin_line_not_empty():
    """零值不能画成 0 高度 —— 那和"这分钟不存在"看不出来。"""
    a = _read("App.vue")
    assert ".spark i.zero" in a
    assert "height: 1px !important" in a


def test_rate_format_units():
    a = _read("App.vue")
    for unit in ("B/s", "KB/s", "MB/s"):
        assert unit in a, unit


# ------------------------------------------------------------ 图标完整性

def test_icons_used_by_new_ui_exist():
    icons = (SRC / "icons.js").read_text(encoding="utf-8")
    defined = set(re.findall(r'^  "([a-z0-9-]+)":', icons, re.M))
    # 图标名写错时 Vue 只会静默画个空 SVG（不报错），必须静态查
    for name in ("arrow-down", "image", "folder-open", "hash", "shield-alert"):
        assert name in defined, f"图标 {name} 没定义"


def test_icons_file_is_well_formed():
    """icons.js 被脚本自动追加过，最怕把图标写到 ICONS 对象外面（整包白屏）。"""
    txt = (SRC / "icons.js").read_text(encoding="utf-8")
    body = txt.split("export const ICONS = {", 1)[1].rsplit("}", 1)[0]
    assert body.count("{") == body.count("}"), "花括号不成对 → 图标被写到对象外面了"
    names = re.findall(r'^  "([a-z0-9-]+)":', body, re.M)
    assert len(names) >= 80, len(names)
    assert len(names) == len(set(names)), "图标名重复"


def test_task_menu_has_reveal_and_copy_id():
    t = _read("views/Tasks.vue")
    assert "在文件夹中打开" in t
    assert "api.taskReveal" in t
    assert "复制任务编号" in t
