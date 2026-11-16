"""R154-F18 护栏：磁盘空间看板（执行方笔记本棒 R212）。

行为断言优先、源码判据兜底；变异自检 .workbuddy/_r212_mutate.py。

覆盖（与 R154-F18 订单逐条对齐）：
- format_size：1024 进制、0/None/负数/NaN/inf 不炸；
- dir_size：递归合计 / 目录不存在或不是目录给 0 / 单文件读不到不拖垮整棵树；
- scan_targets：订单点名的五个目录、顺序、cfg: 与 root: 两种解析；
- collect_usage：fraction 相对最大目录（最大的 1.0）/ 总量 = 各目录之和；
- 快照：落库 round-trip / **同日覆盖（一天一行）** / 逐日补行（无记录给
  None，不当 0）/ delta 与上一个**有记录**的日子比（不跟补行的空日比）；
- snapshot_today_if_missing：幂等（第二次不重复记）；
- 告警：低于阈值给中文（含阈值与建议）/ 高于或关闭检查（0）给空串 /
  测不到剩余空间给空串；
- 桌面：真建 QDialog 驱动（五条 bar / 告警文案 / 趋势表 7 行 / 快照按钮
  真落一行）；
- Web：/api/diskspace 接了路由 + 真调返回 diskspace 口径（不自己写 SQL）；
- 前端：导航项 + VIEWS + render 分发 + diskBarWidth 钳位（node 真跑）。
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from media_resource_manager import diskspace
from media_resource_manager.config import AppConfig
from media_resource_manager.db import Database

DISKSPACE_PY = ROOT / "media_resource_manager" / "diskspace.py"
DIALOG_PY = ROOT / "media_resource_manager" / "gui" / "diskspace_dialog.py"
MW_PY = ROOT / "media_resource_manager" / "gui" / "main_window.py"
WEBUI_PY = ROOT / "media_resource_manager" / "webui.py"
APPJS = ROOT / "media_resource_manager" / "web" / "static" / "js" / "app.js"
INDEX_HTML = ROOT / "media_resource_manager" / "web" / "index.html"


# ==================== 共用夹具 ====================

def _mk_db(tmp_path):
    return Database(tmp_path / "f18.db")


def _mk_cfg(tmp_path) -> AppConfig:
    """五个目录全部指向 tmp_path 下的真目录（图库/待审核/隔离区走 cfg，
    缩略图/模型走 root=tmp_path）。"""
    cfg = AppConfig()
    cfg.db_path = tmp_path / "f18.db"
    cfg.library_dir = tmp_path / "library"
    cfg.review_dir = tmp_path / "review"
    cfg.isolate_dir = tmp_path / "quarantine"
    for d in (cfg.library_dir, cfg.review_dir, cfg.isolate_dir):
        d.mkdir(parents=True, exist_ok=True)
    return cfg


def _write(p: Path, n: int) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * n)


def _fake_handler(db, cfg):
    """不起真服务的 _Handler 桩（与 test_r154_f25_report.py 同款）。"""
    from media_resource_manager import webui

    class _H(webui._Handler):
        def __init__(self):
            self._db = db
            self._cfg = cfg
            self.headers = {}
            self.client_address = ("127.0.0.1", 0)

        @property
        def db(self):
            return self._db

        @property
        def cfg(self):
            return self._cfg

    return _H()


# ==================== format_size ====================

def test_format_size_boundaries():
    assert diskspace.format_size(0) == "0 B"
    assert diskspace.format_size(None) == "0 B"
    assert diskspace.format_size(-5) == "0 B"
    assert diskspace.format_size(512) == "512 B"
    assert diskspace.format_size(1024) == "1.0 KB"
    assert diskspace.format_size(1536) == "1.5 KB"
    assert diskspace.format_size(1024 ** 2) == "1.0 MB"
    assert diskspace.format_size(100 * 1024 ** 2) == "100.0 MB"
    assert diskspace.format_size(5 * 1024 ** 3) == "5.0 GB"
    assert diskspace.format_size(3 * 1024 ** 4) == "3.0 TB"
    # NaN / inf 不许炸（坏数据进来看板照样要开）
    assert diskspace.format_size(float("nan")) == "0 B"
    assert diskspace.format_size(float("inf")) == "0 B"


# ==================== dir_size ====================

def test_dir_size_sums_nested_files(tmp_path):
    root = tmp_path / "tree"
    _write(root / "a.jpg", 100)
    _write(root / "sub" / "b.jpg", 250)
    _write(root / "sub" / "deep" / "c.jpg", 50)
    assert diskspace.dir_size(root) == 400


def test_dir_size_missing_or_not_a_dir_is_zero(tmp_path):
    assert diskspace.dir_size(tmp_path / "nope") == 0
    assert diskspace.dir_size(None) == 0
    f = tmp_path / "a.jpg"
    _write(f, 10)
    assert diskspace.dir_size(f) == 0          # 是文件不是目录
    assert diskspace.dir_size("") == 0


def test_dir_size_unreadable_file_does_not_sink_the_tree(tmp_path, monkeypatch):
    """★ 单个文件 stat 失败只跳过它，其余照算（看板不许整体打不开）。"""
    root = tmp_path / "tree"
    _write(root / "ok1.jpg", 100)
    _write(root / "bad.jpg", 777)
    _write(root / "ok2.jpg", 100)

    real_stat = Path.stat

    def _fake_stat(self, *a, **k):
        if self.name == "bad.jpg":
            raise OSError("file is locked")
        return real_stat(self, *a, **k)

    monkeypatch.setattr(Path, "stat", _fake_stat)
    assert diskspace.dir_size(root) == 200


def test_free_space_of_real_path(tmp_path):
    v = diskspace.free_space(tmp_path)
    assert isinstance(v, int) and v > 0


# ==================== scan_targets / collect_usage ====================

def test_scan_targets_lists_the_five_dirs_in_order(tmp_path):
    cfg = _mk_cfg(tmp_path)
    ts = diskspace.scan_targets(cfg, root=tmp_path)
    assert [t["key"] for t in ts] == ["library", "review", "isolate",
                                      "thumbs", "models"]
    by = {t["key"]: t["path"] for t in ts}
    assert by["library"] == tmp_path / "library"        # cfg: 解析
    assert by["review"] == tmp_path / "review"
    assert by["isolate"] == tmp_path / "quarantine"
    assert by["thumbs"] == tmp_path / "thumbs"          # root: 解析
    assert by["models"] == tmp_path / "models"


def test_collect_usage_fractions_relative_to_largest(tmp_path):
    cfg = _mk_cfg(tmp_path)
    _write(cfg.library_dir / "a.jpg", 3000)
    _write(cfg.review_dir / "b.jpg", 1000)
    _write(cfg.isolate_dir / "c.jpg", 500)
    _write(tmp_path / "thumbs" / "t.jpg", 500)
    _write(tmp_path / "models" / "m.bin", 0)     # 空文件也算 0 字节
    u = diskspace.collect_usage(cfg, root=tmp_path)
    assert u["total_bytes"] == 5000, u
    assert u["total_text"] == "4.9 KB", u["total_text"]
    frac = {d["key"]: d["fraction"] for d in u["dirs"]}
    assert frac["library"] == 1.0                 # 最大目录顶满
    assert abs(frac["review"] - 1 / 3) < 1e-9
    assert abs(frac["isolate"] - 1 / 6) < 1e-9
    assert frac["models"] == 0.0
    # 全是 0 时全部给 0.0，不许除零
    empty = tmp_path / "empty"
    empty.mkdir()
    cfg2 = _mk_cfg(empty)
    u2 = diskspace.collect_usage(cfg2, root=empty)
    assert all(d["fraction"] == 0.0 for d in u2["dirs"])
    assert u2["total_bytes"] == 0


def test_collect_usage_missing_dirs_do_not_crash(tmp_path):
    cfg = AppConfig()                       # 五个目录全都不存在
    cfg.db_path = tmp_path / "x.db"
    u = diskspace.collect_usage(cfg, root=tmp_path / "nothing")
    assert len(u["dirs"]) == 5
    assert u["total_bytes"] == 0
    assert isinstance(u["free_bytes"], int)


# ==================== 快照 / 趋势 ====================

def test_snapshot_roundtrip_and_history_pads_missing_days(tmp_path):
    db = _mk_db(tmp_path)
    cfg = _mk_cfg(tmp_path)
    _write(cfg.library_dir / "a.jpg", 1024)
    u = diskspace.collect_usage(cfg, root=tmp_path)
    diskspace.record_snapshot(db, u["dirs"], free_bytes=u["free_bytes"],
                              day="2026-10-20")
    _write(cfg.library_dir / "b.jpg", 2048)
    u2 = diskspace.collect_usage(cfg, root=tmp_path)
    diskspace.record_snapshot(db, u2["dirs"], free_bytes=u2["free_bytes"],
                              day="2026-10-22")

    hist = diskspace.load_history(db, 7, today="2026-10-22")
    # 7 天逐日补行（订单：7 日趋势；缺日子不许悄悄消失）
    assert [h["date"] for h in hist] == [
        "2026-10-16", "2026-10-17", "2026-10-18", "2026-10-19",
        "2026-10-20", "2026-10-21", "2026-10-22"], hist
    by = {h["date"]: h for h in hist}
    assert by["2026-10-20"]["recorded"] is True
    assert by["2026-10-20"]["total_bytes"] == 1024
    assert by["2026-10-21"]["recorded"] is False
    assert by["2026-10-21"]["total_bytes"] is None     # 不是 0
    assert by["2026-10-21"]["text"] == "无记录"
    assert by["2026-10-22"]["total_bytes"] == 3072
    # delta 与上一个**有记录**的日子比（不跟补行的空日比）
    assert by["2026-10-20"]["delta"] is None            # 窗口内第一个有记录的
    assert by["2026-10-22"]["delta"] == 2048
    assert by["2026-10-22"]["delta_text"] == "+2.0 KB"


def test_same_day_snapshot_keeps_one_row_last_write_wins(tmp_path):
    """★ 一天一行：同一天记八次只会得到八根一样高的柱子（订单原文的坑）。"""
    db = _mk_db(tmp_path)
    diskspace.record_snapshot(db, [{"key": "library", "bytes": 100}],
                              day="2026-10-22")
    diskspace.record_snapshot(db, [{"key": "library", "bytes": 900}],
                              day="2026-10-22")
    rows = db.query("SELECT day, total_bytes FROM disk_snapshots")
    assert len(rows) == 1, rows
    assert rows[0]["total_bytes"] == 900                  # 最后一笔赢
    hist = diskspace.load_history(db, 7, today="2026-10-22")
    assert sum(1 for h in hist if h["recorded"]) == 1


def test_snapshot_today_if_missing_is_idempotent(tmp_path):
    db = _mk_db(tmp_path)
    cfg = _mk_cfg(tmp_path)
    _write(cfg.library_dir / "a.jpg", 4096)
    u1 = diskspace.snapshot_today_if_missing(db, cfg, root=tmp_path,
                                             today="2026-10-22")
    assert u1["total_bytes"] == 4096
    # 目录又长了，但今天已有快照 ⇒ 不重记（大库扫一遍要好几秒）
    _write(cfg.library_dir / "b.jpg", 4096)
    diskspace.snapshot_today_if_missing(db, cfg, root=tmp_path,
                                        today="2026-10-22")
    rows = db.query("SELECT total_bytes FROM disk_snapshots")
    assert len(rows) == 1
    assert rows[0]["total_bytes"] == 4096, rows           # 没有变成 8192
    # 换一天才会记新的
    diskspace.snapshot_today_if_missing(db, cfg, root=tmp_path,
                                        today="2026-10-23")
    assert len(db.query("SELECT day FROM disk_snapshots")) == 2


def test_history_of_broken_db_is_all_missing_not_crash(tmp_path):
    """老库没有 disk_snapshots 表 ⇒ 全「无记录」，看板照样开。"""
    class _Boom:
        def query(self, *a, **k):
            raise RuntimeError("no such table")

    hist = diskspace.load_history(_Boom(), 7, today="2026-10-22")
    assert len(hist) == 7
    assert all(h["recorded"] is False and h["total_bytes"] is None
               for h in hist)


def test_days_param_is_clamped(tmp_path):
    db = _mk_db(tmp_path)
    assert len(diskspace.load_history(db, 0)) == 1
    assert len(diskspace.load_history(db, 999)) == diskspace.MAX_DAYS
    assert len(diskspace.load_history(db, "x")) == diskspace.DEFAULT_DAYS


# ==================== 告警 ====================

def test_alert_below_threshold_is_chinese_and_names_the_threshold():
    free = 100 * 1024 * 1024          # 100 MB < 200 MB
    txt = diskspace.alert_text(free, 200)
    assert "低于" in txt and "200 MB" in txt, txt
    assert "100.0 MB" in txt           # 剩余空间的人类可读值在文案里
    assert "隔离区" in txt             # 给了清理建议，不只是吓人


def test_alert_silent_when_healthy_or_disabled_or_unknown():
    assert diskspace.alert_text(500 * 1024 * 1024, 200) == ""   # 高于阈值
    assert diskspace.alert_text(1, 0) == ""                     # 0=不检查（#54 口径）
    assert diskspace.alert_text(1, None) == ""
    assert diskspace.alert_text(None, 200) == ""                # 测不到剩余空间


def test_headline_names_the_biggest_dir(tmp_path):
    cfg = _mk_cfg(tmp_path)
    _write(cfg.library_dir / "a.jpg", 3000)
    _write(cfg.review_dir / "b.jpg", 1000)
    u = diskspace.collect_usage(cfg, root=tmp_path)
    h = diskspace.headline(u)
    assert "图库" in h and "2.9 KB" in h, h
    # 告警拼在概览后面（顶部一句看完）
    u["alert"] = "测试告警"
    assert "测试告警" in diskspace.headline(u)


# ==================== 桌面对话框（真建） ====================

def test_desktop_dialog_bars_alert_and_history(tmp_path):
    from PySide6.QtWidgets import QApplication

    from media_resource_manager.gui.diskspace_dialog import DiskSpaceDialog
    app = QApplication.instance() or QApplication([])
    assert app is not None

    db = _mk_db(tmp_path)
    cfg = _mk_cfg(tmp_path)
    cfg.min_free_mb = 10 ** 9          # 阈值设成 10 亿 MB ⇒ 必然触发告警
    _write(cfg.library_dir / "a.jpg", 3000)
    _write(cfg.review_dir / "b.jpg", 1000)

    dlg = DiskSpaceDialog(None, db, cfg, root=tmp_path)
    try:
        u = dlg.usage()
        assert len(dlg._bars) == 5, "五个目录五根条（订单点名的清单）"
        # 最大的目录顶满，其余按比例
        assert dlg._bars[0]["bar"].value() == 100
        assert dlg._bars[1]["bar"].value() == 33
        assert dlg._bars[0]["size"].text() == "2.9 KB"
        # 告警：中文 + theme 的 {danger} 语义位（换肤/深浅都跟随；
        # 原生模式下 css() 返 "" 是 theme 的口径，见 theme.css docstring）
        assert "低于" in dlg.lb_alert.text()
        from media_resource_manager.gui import theme
        sheet = dlg.lb_alert.styleSheet()
        assert (theme.PALETTE["dark"]["danger"] in sheet
                or theme.PALETTE["light"]["danger"] in sheet), sheet
        assert "1000000000 MB" in dlg.lb_free.text()
        # 打开时就已记上今天的快照（「每日一快照」不要求用户点按钮）
        rows = db.query("SELECT day, total_bytes FROM disk_snapshots")
        assert len(rows) == 1 and rows[0]["total_bytes"] == 4000
        # 趋势表 7 行，最后一行是今天
        assert dlg.tbl.rowCount() == 7
        assert dlg.tbl.item(6, 0).text().endswith("（今天）")
        assert dlg.tbl.item(6, 1).text() == "3.9 KB"

        # 「立即记录快照」：目录又长了，**先重扫**再记一笔（同日覆盖）。
        # 用户常常是刚清完空间才点它，记打开时的旧用量等于没记。
        _write(cfg.library_dir / "c.jpg", 1000)
        dlg.record_now()
        rows = db.query("SELECT total_bytes FROM disk_snapshots")
        assert len(rows) == 1 and rows[0]["total_bytes"] == 5000
        assert dlg.usage()["total_bytes"] == 5000        # 界面也刷新成新值
    finally:
        dlg.close()


def test_desktop_dialog_without_db_still_opens(tmp_path):
    """db=None（只读场景）不许炸：没有快照功能，界面照常。"""
    from PySide6.QtWidgets import QApplication

    from media_resource_manager.gui.diskspace_dialog import DiskSpaceDialog
    app = QApplication.instance() or QApplication([])
    cfg = _mk_cfg(tmp_path)
    dlg = DiskSpaceDialog(None, None, cfg, root=tmp_path)
    try:
        assert len(dlg._bars) == 5
        assert dlg.tbl.rowCount() == 0
    finally:
        dlg.close()


def test_dialog_uses_theme_tokens_not_hex_literals():
    """告警色走 theme 的 {danger} 语义位：硬编码 hex 换皮肤不跟随
    （test_p14b_skin_convergence 全局护栏，R212 首版真被它抓到过）。"""
    src = DIALOG_PY.read_text(encoding="utf-8")
    assert "{danger}" in src, "告警色没用 theme 的 {danger} 语义位"
    assert "from .theme import style as _tstyle" in src
    assert "#b3261e" not in src


def test_main_window_menu_and_opener_are_wired():
    mw = MW_PY.read_text(encoding="utf-8")
    assert 'self.act_diskspace.triggered.connect(self.show_disk_space)' in mw
    assert 'def show_disk_space(self):' in mw
    assert 'QAction("磁盘空间看板…", self)' in mw
    # 打开器必须把 cfg 传进去（阈值联动靠它）
    assert "DiskSpaceDialog(self, self.db, self.cfg)" in mw


def test_dialog_uses_the_pure_layer_not_its_own_scan():
    """对话框不许自己 os.walk / 自己拼目录表（两端口径分家）。"""
    src = DIALOG_PY.read_text(encoding="utf-8")
    assert "from .. import diskspace" in src
    assert "os.walk" not in src, "对话框自己扫目录了（口径会跟网页端分家）"
    assert "shutil.disk_usage" not in src
    assert "diskspace.load_history(self._db)" in src


# ==================== Web ====================

def test_diskspace_endpoint_is_routed():
    src = WEBUI_PY.read_text(encoding="utf-8")
    assert 'if path == "/api/diskspace":' in src, \
        "/api/diskspace 没接路由（写了函数不接 = 白写）"
    assert "self._api_diskspace(u)" in src


def test_web_api_serves_the_same_pure_layer():
    src = WEBUI_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_api_diskspace":
            fn = node
    assert fn is not None, "webui.py 里没有 _api_diskspace"
    calls = set()
    for n in ast.walk(fn):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if isinstance(f, ast.Attribute):
            calls.add(f.attr)
        elif isinstance(f, ast.Name):
            calls.add(f.id)
    for want in ("snapshot_today_if_missing", "load_history",
                 "headline", "format_size"):
        assert want in calls, f"_api_diskspace 没调 diskspace.{want}"
    # ⚠ 不要求直接出现 collect_usage：snapshot_today_if_missing 内部就是
    #   collect_usage（一天只扫一遍的保证），Web 再直接调一遍等于双扫。
    #   防「第二套口径」的真判据是下面这条：一个 SQL 都不许自己写。
    seg = ast.get_source_segment(src, fn) or ""
    assert "SELECT" not in seg.upper(), "_api_diskspace 自己写了 SQL（第二套口径）"


def test_web_api_returns_one_payload(tmp_path):
    from urllib.parse import urlparse

    db = _mk_db(tmp_path)
    cfg = _mk_cfg(tmp_path)
    cfg.min_free_mb = 200
    _write(cfg.library_dir / "a.jpg", 2048)
    _write(cfg.review_dir / "b.jpg", 1024)

    h = _fake_handler(db, cfg)
    out = h._api_diskspace(urlparse("/api/diskspace"))
    assert out["ok"] is True, out
    assert [d["key"] for d in out["dirs"]] == [
        "library", "review", "isolate", "thumbs", "models"]
    assert out["total_bytes"] == 3072
    assert out["total_text"] == "3.0 KB"
    assert out["threshold_mb"] == 200
    assert out["free_text"] != "未知"
    assert len(out["history"]) == 7
    assert out["history"][-1]["recorded"] is True     # 打开即记快照
    assert out["headline"].startswith("各目录共")
    # 告警：阈值 200MB、真机剩余空间远大于它 ⇒ 空串（不许误报）
    assert out["alert"] == ""
    # days 参数透传
    out2 = h._api_diskspace(urlparse("/api/diskspace?days=3"))
    assert len(out2["history"]) == 3
    # 坏参数不许 500
    out3 = h._api_diskspace(urlparse("/api/diskspace?days=abc"))
    assert out3["ok"] is True and len(out3["history"]) == 7


def test_web_api_alert_fires_when_below_threshold(tmp_path):
    """阈值设极大 ⇒ 必然触发中文告警（订单：超阈值中文告警）。"""
    from urllib.parse import urlparse

    db = _mk_db(tmp_path)
    cfg = _mk_cfg(tmp_path)
    cfg.min_free_mb = 10 ** 9
    h = _fake_handler(db, cfg)
    out = h._api_diskspace(urlparse("/api/diskspace"))
    assert out["ok"] is True
    assert "低于" in out["alert"] and "1000000000 MB" in out["alert"], out["alert"]
    assert "低于" in out["headline"]


def test_web_api_broken_db_degrades_not_crashes(tmp_path):
    """★ 库挂了（锁 / 缺表）看板照常开：目录照扫，趋势全「无记录」。

    与 F25「坏库不崩」同口径：报错页等于这个功能在用户最需要它的时刻
    （盘慢了 / 库锁了）反而打不开。
    """
    from urllib.parse import urlparse

    class _Boom:
        def execute(self, *a, **k):
            raise RuntimeError("db is locked")

        def query(self, *a, **k):
            raise RuntimeError("db is locked")

    h = _fake_handler(_Boom(), _mk_cfg(tmp_path))
    out = h._api_diskspace(urlparse("/api/diskspace"))
    assert out["ok"] is True, out
    assert len(out["dirs"]) == 5
    assert len(out["history"]) == 7
    assert all(not hh["recorded"] for hh in out["history"])

def test_snapshot_table_exists_in_fresh_db(tmp_path):
    """新库自带 disk_snapshots（老库靠 executescript 补建，同一条路）。"""
    db = _mk_db(tmp_path)
    cols = {r["name"] for r in db.query("PRAGMA table_info(disk_snapshots)")}
    assert {"day", "total_bytes", "detail", "free_bytes",
            "recorded_at"} <= cols, cols
    # day 是主键（一天一行的物理保证）
    pk = [r["name"] for r in db.query("PRAGMA table_info(disk_snapshots)")
          if r["pk"]]
    assert pk == ["day"], pk
