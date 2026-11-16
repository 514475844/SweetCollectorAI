"""R83 · PART2 第 48 项「压力阈值」的常驻护栏。

═══ 背景（实测数据来自 tools/r83_stress_data.py / r83_stress_gui.py /
   r83_stress_table.py，2026-10-12 R83，CPU-only 笔记本 offscreen）═══
第 48 项要求「图库/审核/任务/过滤器每一项单独压测，给出具体数字」。
探针实测（20 万图 / 2000 任务 / 1000 规则内**零崩溃**），卡顿阈值：
  · 图库：keyset 分页首页 0.8ms、深翻页（偏移 2 万）5.3ms、走满 100 页 88ms
    ⇒ 分页设计有效，**不许退化成 OFFSET**（name 排序走 OFFSET 是有意的：
    path 可空可重复，键集挂不上；18ms→187ms 线性但 <0.2s）。
  · 审核：pending(50) 1.6ms、count_pending 49ms@20 万。
  · 任务：cards 模式全量重建 ~21ms/任务（2000 条 = 42.8s，**超线性恶化**）；
    table 模式刷新 2000 条 = 5ms（差 8400 倍）；稳态刷新 12ms（指纹 no-op）。
  · 切皮肤：232 任务 207~385ms ⇒ 2000 任务 18~22s（**超线性**，
    由存活的 TaskRow 数驱动，与当前皮肤无关）。
  · 过滤器：_hit_blacklist 10 规则 20µs ⇒ 1000 规则 2.1ms/次（线性）。

═══ 本文件押的契约 ═══
把上面每条路径的**数量级**钉成测试。阈值全部留 10 倍以上余量
（防抖），但任何「分页退化 / N+1 爆炸 / 每任务成本超线性恶化」都会红。

⚠ R67 教训：monkeypatch / 环境变量是进程级全局 ⇒ 一律用 pytest 的
  monkeypatch（自动还原），本文件末尾还有一条「不泄漏」元测试。
⚠ 数据 seeder 与探针**共用同一份**（tools/r83_stress_common.py）——
  R63-D3：判据/夹具与被护对象分家 = 空壳。
⚠ 计时断言在满载机器上会漂移 ⇒ 阈值取实测值 10~250 倍余量，
  且全部单次调用（不循环取平均，避免把抖动平均掉）。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# 与 tools/r83_stress_*.py 同一份 seeder（R63-D3：不许各写一份）
sys.path.insert(0, str(ROOT / "tools"))

from r83_stress_common import seed_images, seed_tasks  # noqa: E402

N_TASKS_DATA = 300      # 数据层：300 任务 / 5000 图
N_IMG_DATA = 5000
N_TASKS_GUI = 150       # GUI 层：150 任务 / 2000 图（再大 full suite 跑不动）
N_IMG_GUI = 2000


# ---------------------------------------------------------------- 夹具
@pytest.fixture()
def data_home(tmp_path, monkeypatch):
    """函数级隔离：MRM_HOME 指向临时目录（monkeypatch 自动还原）。"""
    monkeypatch.setenv("MRM_HOME", str(tmp_path))
    monkeypatch.setenv("MRM_NO_MIGRATE", "1")
    return tmp_path


@pytest.fixture()
def sdb(data_home):
    """数据层压测库：300 任务 + 5000 图（分布同探针）。"""
    from media_resource_manager.db import Database
    db = Database(data_home / "mrm.db")
    seed_tasks(db, N_TASKS_DATA)
    seed_images(db, N_IMG_DATA, N_TASKS_DATA)
    return db


@pytest.fixture()
def web_srv(data_home, sdb, monkeypatch):
    """真起 WebServer（R68 教训：直调方法绕过了路由层，必须走 socket）。"""
    import media_resource_manager.config as cfgmod
    from media_resource_manager import webui
    from media_resource_manager.config import AppConfig

    cfg = AppConfig()
    cfg.db_path = str(sdb.db_path)
    cfg.web_password = "pw123"
    # R211：R152-E5 把 web_lan_open 的默认翻成了 False（安全默认，是对的），
    # 于是 127.0.0.1 不再免密 ⇒ 本文件的裸 GET（_http_get 不带凭证）一律 401。
    # 本文件压的是「/api/tasks 快不快」，不是鉴权；回环地址本就属局域网那档，
    # 这里显式打开。**产品默认保持关闭不动** —— 那正是 E5 要的。
    cfg.web_lan_open = True
    cfg.web_bind = "127.0.0.1"
    cfg.web_port = 0
    # monkeypatch.setattr 自动还原（R67：不还原会污染别的测试）
    monkeypatch.setattr(cfgmod, "save_config", lambda *a, **k: None)
    srv = webui.WebServer(cfg, sdb, queue=object())
    ok, msg = srv.start()
    assert ok, f"WebServer 起不来: {msg}"
    base = f"http://127.0.0.1:{cfg.web_port}"
    try:
        yield base
    finally:
        try:
            srv.stop()
        except Exception:
            pass


def _http_get(base: str, path: str, timeout: float = 30.0):
    import json
    import urllib.request
    with urllib.request.urlopen(base + path, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


# ---------------------------------------------------------------- 图库
def test_gallery_keyset_first_page_fast(sdb):
    """图库首页 < 100ms（实测 0.8ms@1 万图）。"""
    from media_resource_manager.repo import list_images_page
    t0 = time.perf_counter()
    rows, cur, more = list_images_page(sdb, limit=200)
    dt = time.perf_counter() - t0
    assert len(rows) == 200, "首页应满 200 行"
    assert dt < 0.1, f"图库首页 {dt*1000:.1f}ms ≥ 100ms —— 索引/分页可能被改坏"


def test_gallery_deep_page_not_slower_than_first(sdb):
    """深翻页（游标走到偏移 2500）单页 < 100ms。

    这条是 **OFFSET 退化护栏**：keyset 的承诺是「第 N 页与第 1 页同量级」。
    谁把 list_images_page 改回 `LIMIT ? OFFSET ?`，这条就红。
    """
    from media_resource_manager.repo import list_images_page
    cur = None
    for _ in range(13):                      # 13×200 ≈ 偏移 2500
        _rows, cur, _more = list_images_page(sdb, limit=200, cursor=cur)
    assert cur is not None, "5000 张图翻 13 页不该到头"
    t0 = time.perf_counter()
    rows, _cur, _more = list_images_page(sdb, limit=200, cursor=cur)
    dt = time.perf_counter() - t0
    assert len(rows) == 200
    assert dt < 0.1, f"深翻页单页 {dt*1000:.1f}ms ≥ 100ms —— 疑似退化成 OFFSET"


def test_gallery_count_fast(sdb):
    """图库 COUNT（首页渲染要用的总数）< 100ms（实测 3.5ms@5 万）。"""
    t0 = time.perf_counter()
    rows = sdb.query("SELECT COUNT(*) AS c, MAX(id) AS m FROM images "
                     "WHERE path IS NOT NULL AND blacklisted=0 "
                     "AND in_quarantine=0")
    dt = time.perf_counter() - t0
    assert rows[0]["c"] > 0
    assert dt < 0.1, f"COUNT {dt*1000:.1f}ms ≥ 100ms"


def test_list_images_page_uses_keyset_not_offset():
    """AST 结构护栏：`list_images_page` 必须是 keyset（游标）分页。

    ⚠ 为什么单有条文：计时护栏在 5000 图尺度上**分不出** keyset 与 OFFSET
    （OFFSET 2600 行只要几毫秒），探针是在 20 万图尺度上才量出差别
    （走满 100 页 88ms 恒定 vs OFFSET 线性涨）。结构不变量则与数据量无关：
    只要函数体里出现 OFFSET 或丢掉 cursor 条件，就是退化。
    ⚠ R69/R73 教训：ast.unparse 会把 **docstring 一起**读出来（注释里
    大量引用历史写法）⇒ 先剥掉 docstring 再拼可执行语句。
    """
    import ast
    src = (ROOT / "media_resource_manager" / "repo.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef)
               and n.name == "list_images_page"), None)
    assert fn is not None, "找不到 list_images_page"
    body = [n for n in fn.body
            if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                    and isinstance(n.value.value, str))]
    code = ast.unparse(ast.Module(body=body, type_ignores=[]))
    assert "OFFSET" not in code.upper(), \
        "list_images_page 里出现 OFFSET —— keyset 退化成偏移分页，" \
        "深翻页会随页码线性变慢（20 万图实测 88ms→秒级）"
    assert "cursor" in code, "list_images_page 不再用 cursor —— 键集分页的灵魂"
    assert "AND id" in code, "找不到游标比较条件「AND id {op} ?」"


# ---------------------------------------------------------------- 审核
def test_review_count_pending_fast(sdb):
    """待审核总数 < 150ms（实测 49ms@20 万，全表 COUNT 线性但可控）。"""
    from media_resource_manager.review.queue import ReviewQueue
    q = ReviewQueue(sdb, sdb.db_path.parent / "isolate")
    t0 = time.perf_counter()
    n = q.count_pending()
    dt = time.perf_counter() - t0
    assert n > 0, "夹具里应有待审核图"
    assert dt < 0.15, f"count_pending {dt*1000:.1f}ms ≥ 150ms"


def test_review_pending_page_fast(sdb):
    """审核队列取一页 < 50ms（实测 1.6ms，含每行一次 get_image）。"""
    from media_resource_manager.review.queue import ReviewQueue
    q = ReviewQueue(sdb, sdb.db_path.parent / "isolate")
    t0 = time.perf_counter()
    items = q.pending(limit=50)
    dt = time.perf_counter() - t0
    assert 0 < len(items) <= 50
    assert dt < 0.05, f"pending(50) {dt*1000:.1f}ms ≥ 50ms"


# ---------------------------------------------------------------- 任务（Web）
def test_api_tasks_fast_with_many_tasks(web_srv):
    """/api/tasks < 2s（实测 8ms@300 任务；2000 任务/20 万图 = 615ms）。

    这条是 **N+1 爆炸护栏**：_api_tasks 对每个任务调一次 task_progress。
    若哪天 task_progress 变成「每任务一次全表扫」，300 任务 × 5000 图
    ≈ 4.5s > 2s ⇒ 红。
    """
    t0 = time.perf_counter()
    st, obj = _http_get(web_srv, "/api/tasks")
    dt = time.perf_counter() - t0
    assert st == 200
    assert len(obj) == N_TASKS_DATA
    assert dt < 2.0, f"/api/tasks {dt:.2f}s ≥ 2s —— N+1 疑似爆炸"


# ---------------------------------------------------------------- 过滤器
def test_blacklist_filter_bounded_per_call(sdb):
    """_hit_blacklist 500 规则 < 20ms/次（实测 1.06ms，严格线性）。

    线性本身不是缺陷（缺陷是超线性）；这条钉住「别变成每条规则一次
    全URL扫描之外的平方级行为」。>512 规则时 re 缓存抖动是已知现象，
    优化归第 49 项，这里只防劣化。
    """
    from media_resource_manager.engine import _hit_blacklist
    rules = [f"word{i}" for i in range(250)] + [f"词{i}" for i in range(250)]
    url = "https://pictoa.example/albums/12345/cover.jpg"
    t0 = time.perf_counter()
    for _ in range(50):
        assert _hit_blacklist(url, ["白丝", "制服"], rules) is False
    per = (time.perf_counter() - t0) / 50
    assert per < 0.02, f"500 规则 {per*1000:.2f}ms/次 ≥ 20ms"


# ---------------------------------------------------------------- GUI 层
@pytest.fixture(scope="module")
def gui_win(tmp_path_factory):
    """模块级真窗口：150 任务 + 2000 图，先 violet_look（cards）。

    ⚠ MRM_HOME 必须在 MainWindow() 之前设（AppConfig 在构造时读
      user_data_dir）—— 用 dict 手动设/还原，因为 monkeypatch 是
      函数级，撑不到模块级夹具外面。
    """
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    tmp = tmp_path_factory.mktemp("r83gui")
    old = {k: os.environ.get(k) for k in ("MRM_HOME", "MRM_NO_MIGRATE")}
    os.environ["MRM_HOME"] = str(tmp)
    os.environ["MRM_NO_MIGRATE"] = "1"
    try:
        from media_resource_manager.db import Database
        from media_resource_manager.gui import main_window as mwmod
        db = Database(tmp / "mrm.db")
        seed_tasks(db, N_TASKS_GUI)
        seed_images(db, N_IMG_GUI, N_TASKS_GUI)
        w = mwmod.MainWindow()
        w.set_skin("violet_look")
        w.resize(1400, 900)
        w.show()
        QApplication.processEvents()
        try:
            yield w
        finally:
            try:
                w.close()
            except Exception:
                pass
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        # ⚠ 还原必须**在本夹具内验证**：模块级夹具的 env 在整个模块期间
        # 都是设着的，模块内的元测试看不到「还原后」的状态（R67 教训的
        # 正确形态：在 finally 里当场断言，失败就让模块 teardown 报错）。
        leaked = [k for k in ("MRM_HOME", "MRM_NO_MIGRATE")
                  if os.environ.get(k) != old.get(k)]
        assert not leaked, f"模块级夹具泄漏环境变量: {leaked} —— 会污染后续测试文件"


def test_table_mode_refresh_instant_and_no_row_widgets(gui_win):
    """classic（表格）模式刷 150 任务 < 1s 且**不新建 TaskRow 控件**。

    后半句是**关系断言**（不是计时）：表格模式走 QTableView+model，
    刷新 2000 条实测 5ms；卡片模式每行一个 QWidget，2000 条 = 42.8s。
    「表格刷新不得新建行控件」正是这个 8400 倍差距的结构根源。

    ⚠ 不变量取「刷新前后 TaskRow 存量不变」而不是「经典下为 0」：
    切到 classic 后 cards 那套是**故意保留**的（隐藏 + 脏标记，
    切回去由 `_apply_layout_visibility` 补刷，见 _refresh_task_table 头注）。
    要求它为 0 是把设计反过来，是我第一版的判据错（R53：先怀疑仪器）。
    """
    from PySide6.QtWidgets import QApplication
    from media_resource_manager.gui.main_window import TaskRow
    from media_resource_manager.repo import list_tasks
    gui_win.set_skin("classic")
    QApplication.processEvents()
    tasks = list_tasks(gui_win.db)
    assert len(tasks) == N_TASKS_GUI
    before = len(gui_win.task_list.findChildren(TaskRow))
    t0 = time.perf_counter()
    gui_win._refresh_task_table(tasks)
    QApplication.processEvents()
    dt = time.perf_counter() - t0
    after = len(gui_win.task_list.findChildren(TaskRow))
    assert dt < 1.0, f"表格刷新 {dt:.2f}s ≥ 1s（实测 5ms@2000 任务）"
    assert after == before, \
        f"表格刷新把 TaskRow 从 {before} 变成 {after} —— 表格模式不该建行控件"


def test_cards_rebuild_stays_linear(gui_win):
    """cards 模式全量重建 150 任务：总耗时 < 8s 且**每任务 < 55ms**。

    实测 ~21ms/任务（2000 条 = 42.8s）。超线性恶化（如每次重建都重算
    全部行签名、或布局反复 activate）会让每任务成本随 N 上涨 ⇒ 红。
    """
    from PySide6.QtWidgets import QApplication
    from media_resource_manager.repo import list_tasks
    gui_win.set_skin("violet_look")
    QApplication.processEvents()
    tasks = list_tasks(gui_win.db)
    gui_win._task_row_sigs = {}
    t0 = time.perf_counter()
    gui_win._rebuild_task_list(tasks)
    QApplication.processEvents()
    dt = time.perf_counter() - t0
    per = dt / max(1, len(tasks))
    assert dt < 8.0, f"cards 重建 {dt:.1f}s ≥ 8s（实测 ~21ms/任务）"
    assert per < 0.055, f"每任务 {per*1000:.1f}ms ≥ 55ms —— 超线性恶化"


def test_skin_switch_fast_at_150_tasks(gui_win):
    """五个皮肤逐个切，每个 < 4s（实测 207~385ms@232 任务）。

    ⚠ 实测超线性：2000 任务时切皮肤 18~22s。根因是「存活的 TaskRow 数」
    （切皮肤要重刷全部已登记控件），与当前皮肤无关。这条钉住小规模下的
    数量级；大规模塌陷归第 49/50 项优化，届时本阈值随优化结果收紧。
    """
    from PySide6.QtWidgets import QApplication
    from media_resource_manager.gui import theme
    worst = 0.0
    for key in theme.PRESETS:
        t0 = time.perf_counter()
        gui_win.set_skin(key)
        QApplication.processEvents()
        dt = time.perf_counter() - t0
        worst = max(worst, dt)
        assert dt < 4.0, f"切到 {key} {dt:.1f}s ≥ 4s（实测 207~385ms@232 任务）"
    gui_win.set_skin("violet_look")
    QApplication.processEvents()


def test_gallery_load_fast_at_2k_images(gui_win):
    """图库翻页重绘 < 1.5s（实测 29~37ms@20 万图）。"""
    from PySide6.QtWidgets import QApplication
    gui_win._gal_sig = None
    gui_win._gal_base_sig = None
    gui_win._gal_page = 0
    t0 = time.perf_counter()
    gui_win.load_gallery()
    QApplication.processEvents()
    dt = time.perf_counter() - t0
    assert dt < 1.5, f"load_gallery {dt:.2f}s ≥ 1.5s"


# ---------------------------------------------------------------- 元测试
def test_this_file_restores_function_scoped_state():
    """R67 防复发：函数级 monkeypatch（save_config）必须已还原。

    ⚠ 只查函数级那半：MRM_HOME 由**模块级** gui_win 持有，模块内看不到
    还原后的状态，它的还原在 gui_win 的 finally 里当场断言（见上）。
    """
    import media_resource_manager.config as cfgmod
    assert getattr(cfgmod.save_config, "__name__", "") != "<lambda>", \
        "save_config 还是被替换成的 lambda —— 会污染别的测试（R67 教训）"
