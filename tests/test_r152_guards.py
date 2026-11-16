"""R152 已落地条目护栏（2026-10-17 下午棒续）。

订单纪律：「每条修完配测试（优先行为断言，防"假闭环"）」。
本文件覆盖本轮已修条目中此前没有行为断言的部分：

  A1 模型下载回调断裂   —— _on_bg_result 按 (cb,res,err) 解包分发（行为）
  A2 图库排序路径崩溃   —— _gal_sort_key 经实例调用不再把 self 当首参（行为）
  A3 灯箱不喜欢 NameError —— 闭包 scope="task"（源码判据 + 既有行为测试）
  A4 定时任务提前触发+双跑 —— _parse_schedule 只回今天时刻/不夹紧（行为）
  A5 SSIM 隔离覆盖+不回写 —— 唯一名 + images 表回写（行为）
  A6 恢复备份覆盖在用库   —— db.backup() 一致快照机制（行为）
  A7 启动全表扫盘        —— refresh_stats 隐藏即 return / _bg_submit（源码）
  A8 适配器按钮重复连接   —— 各只 connect 一次（源码）
  A9 编辑已删除任务      —— 「任务不存在」不再退化成新建（源码）
  B4 tidy 误扫用户目录    —— clean_scan_dirs 白名单（行为）
  E1 socket timeout      —— _Handler.timeout=30 + 满载 503（行为）
  E2 _body 超 1MB        —— 读干+断连（行为）
  E4 安全响应头          —— nosniff/CSP 等真发出去（行为）
"""
import io
import json
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from media_resource_manager import webui
from media_resource_manager.db import Database
from media_resource_manager.gui import main_window as mw_mod
from media_resource_manager.tidy import clean_scan_dirs

MW_SRC = (Path(_ROOT) / "media_resource_manager" / "gui" /
          "main_window.py").read_text(encoding="utf-8")
RT_SRC = (Path(_ROOT) / "media_resource_manager" / "gui" /
          "review_tab.py").read_text(encoding="utf-8")
WEBUI_SRC = (Path(_ROOT) / "media_resource_manager" /
             "webui.py").read_text(encoding="utf-8")


@pytest.fixture()
def qapp(monkeypatch):
    from PySide6.QtWidgets import QApplication, QMessageBox
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    return app


def _bare_mw():
    """不跑 __init__ 的 MainWindow 壳（只测与 self 状态无关的类方法）。

    ⚠ 不能走 object.__new__：QMainWindow 子类重写了 __new__（sip 侧），
    object.__new__(MainWindow) 直接 TypeError。
    """
    return mw_mod.MainWindow.__new__(mw_mod.MainWindow)


def _block(src: str, name: str) -> str:
    """取类中某个方法的源码块（到下一个同级 def 为止）。"""
    i = src.index(f"def {name}(")
    j = src.find("\n    def ", i + 1)
    return src[i:j if j > 0 else len(src)]


def _code_only(blk: str) -> str:
    """去掉注释行再断言——修复说明里常含被修掉的关键字（scope=scope 等），
    不剥注释会「自己写的文档自己红」（实测踩到三次）。"""
    return "\n".join(l for l in blk.splitlines()
                     if not l.strip().startswith("#"))


def _mk_task(db, name, entry_urls, output_dir):
    cur = db.execute(
        "INSERT INTO tasks (name, entry_urls, output_dir, status) "
        "VALUES (?,?,?,?)",
        (name, json.dumps(entry_urls), str(output_dir), "idle"))
    return int(cur.lastrowid)


# ------------------------------------------------------------------ A1

def test_a1_on_bg_result_dispatches_tuple_payload():
    """槽必须按 (cb, res, err) 三元组解包：成功调回调、失败只记日志。"""
    calls, logs = [], []
    dummy = SimpleNamespace(_closing=False, db=None,
                            log_signal=SimpleNamespace(emit=logs.append))
    ok_cb = lambda r: calls.append(("ok", r))      # noqa: E731
    mw_mod.MainWindow._on_bg_result(dummy, (ok_cb, "RES", None))
    assert calls == [("ok", "RES")]
    mw_mod.MainWindow._on_bg_result(dummy, (ok_cb, None, ValueError("boom")))
    assert calls == [("ok", "RES")], "err 非 None 时不该调回调"
    assert any("boom" in m for m in logs), "失败原因必须进日志"


def test_a1_all_emit_sites_use_standard_tuple():
    """模型下载进度/完成两处 + _bg_submit 一处，全部发三元组（A1 根因是
    裸 lambda：槽按三元组解包直接 ValueError，进度条/弹窗全死）。"""
    assert MW_SRC.count("bg_result.emit((") == 3
    assert "bg_result.emit(lambda" not in MW_SRC


# ------------------------------------------------------------------ A2

def test_a2_gal_sort_key_callable_via_instance():
    """经实例调用（调用方 :8926 的 self._gal_sort_key(r, srt) 同款姿势）。

    缺 @staticmethod 时实例会被当成第一个位置参数塞进 r，
    ``r.get(...)`` 在 MainWindow 上 AttributeError —— 被 load_gallery 的
    except 吞成「读取失败」，切排序键整个图库加载不出来。
    """
    inst = _bare_mw()
    row = {"id": 7, "score": 0.5, "width": 10, "height": 20,
           "source_url": "https://x.com/a"}
    assert inst._gal_sort_key(row, "score") == 0.5
    assert inst._gal_sort_key(row, "res") == 200
    assert inst._gal_sort_key(row, "site") == "https://x.com/a"
    assert inst._gal_sort_key(row, "whatever") == 7     # 默认按 id


# ------------------------------------------------------------------ A3

def test_a3_decide_closure_passes_scope_task():
    """灯箱不喜欢闭包曾引用未定义 scope（NameError 被灯箱 except: pass 吞掉
    ⇒ 不学习/不入排除词/不删除却照样翻页）。现在必须显式 scope="task"。

    ⚠ 只查灯箱闭包块：网格 decide() 里有自己合法的 scope 局部变量，
    全文一刀切「scope=scope 不存在」会误伤（实测踩到）。
    """
    blk = _code_only(_block(RT_SRC, "on_decide"))
    assert 'scope="task"' in blk
    assert "scope=scope" not in blk


# ------------------------------------------------------------------ A4

def test_a4_parse_schedule_daily_is_today_not_clamped():
    """daily 只返回「今天」的时刻——哪怕是已经过去的时间。

    旧实现返回「最近一个 ≤now 的 08:30」⇒ 下午新建的 08:30 任务一启动
    就命中昨天时间立即入队，到点当天再跑一次（双跑）。
    """
    from datetime import datetime
    inst = _bare_mw()
    kind, when, err = inst._parse_schedule("08:30")
    assert err is None and kind == "daily"
    today = datetime.now()
    assert when.date() == today.date()
    assert (when.hour, when.minute) == (8, 30)
    # ★ 这里原本还有一条 `assert when <= today` —— 那是一条**凌晨定时
    #   炸弹**：00:00–08:30 之间跑测试，今天的 08:30 还没到 ⇒ when > today
    #   ⇒ 假红（R158 棒 00:59 实测命中，与被测代码无关）。
    #   它想护的其实是「别夹紧成昨天」，而上面那句
    #   `when.date() == today.date()` 已经把这条钉死了（真夹紧到昨天时
    #   日期就不相等）。删掉依赖墙上时钟的那条不是放水：真回归照样红。


def test_a4_parse_schedule_errors_and_once():
    from datetime import datetime
    inst = _bare_mw()
    assert inst._parse_schedule("") [2] == "empty"
    assert inst._parse_schedule("   ")[2] == "empty"
    assert inst._parse_schedule("25:00")[2] is not None
    assert inst._parse_schedule("08:60")[2] is not None
    assert inst._parse_schedule("瞎写")[2] is not None
    kind, when, err = inst._parse_schedule("2026-10-01 08:30")
    assert err is None and kind == "once"
    assert when == datetime(2026, 10, 1, 8, 30)


def test_a4_created_local_converts_utc_to_local():
    """created_at 是 sqlite UTC；last_run_at 是本地。比较前必须换算，
    否则 8 小时时差会让「本周期已跑过」判断失灵。"""
    from datetime import datetime, timedelta
    inst = _bare_mw()
    off = datetime.now() - datetime.utcnow()
    got = inst._created_local("2026-10-17 00:00:00")
    assert got is not None
    # 方向判据：本地 = UTC + 本地偏移（UTC+8 环境必须晚于输入）。
    # 两次取当前时间之间会隔几微秒，精确相等在本机上必然抖动，
    # 给 1 毫秒容差；换算方向仍是硬断言，不会把真正的 8 小时时差放过。
    delta = (got - datetime(2026, 10, 17, 0, 0, 0)) - off
    assert abs(delta) < timedelta(milliseconds=1), delta


def test_a4_grace_constant_present():
    assert mw_mod.MainWindow._SCHED_GRACE_S == 600


# ------------------------------------------------------------------ A5

def test_a5_similar_dialog_quarantine_uses_unique_name_and_writes_db(
        tmp_path, qapp, monkeypatch):
    """隔离必须：① 唯一名（不覆盖隔离区已有文件）② 回写 images 表。"""
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "information",
                        staticmethod(lambda *a, **k: None))
    lib = tmp_path / "lib"
    lib.mkdir()
    iso = tmp_path / "iso"
    iso.mkdir()
    a, b = lib / "a.jpg", lib / "b.jpg"
    a.write_bytes(b"AAA")
    b.write_bytes(b"BBB")
    (iso / "a.jpg").write_bytes(b"OLD")        # 隔离区已有同名文件

    db = Database(tmp_path / "mrm.db")
    tid = _mk_task(db, "t", ["https://x.com/"], lib)
    db.execute("INSERT INTO images (task_id, source_url, path) VALUES (?,?,?)",
               (tid, "https://x.com/a", str(a)))

    done = []
    from media_resource_manager.gui.ssim_dialog import SimilarDialog
    dlg = SimilarDialog([([a, b], b)], iso, lambda: done.append(1), db=db)
    dlg.apply()

    assert (iso / "a.jpg").read_bytes() == b"OLD", "隔离区旧文件被静默覆盖了"
    assert (iso / "a(1).jpg").read_bytes() == b"AAA", "没用唯一名"
    # 回写：旧 path 行更新成新 path + in_quarantine=1
    assert db.query("SELECT id FROM images WHERE path=?", (str(a),)) == []
    rows = db.query("SELECT in_quarantine, needs_review FROM images "
                    "WHERE path=?", (str(iso / "a(1).jpg"),))
    assert rows and rows[0]["in_quarantine"] == 1
    assert rows[0]["needs_review"] == 0
    assert done == [1]


# ------------------------------------------------------------------ A6

def test_a6_db_backup_yields_consistent_snapshot(tmp_path):
    """A6 依赖的机制：在线 backup() 拿到的一致快照必须含未并回主库的改动
    （WAL 模式下直接复制 .db 文件会丢最近事务——原缺陷根源）。"""
    db = Database(tmp_path / "mrm.db")
    _mk_task(db, "t1", ["https://x.com/"], tmp_path)
    snap = tmp_path / "snap" / "mrm.db"
    db.backup(snap)
    db.close()
    db2 = Database(snap)
    rows = db2.query("SELECT name FROM tasks")
    assert [r["name"] for r in rows] == ["t1"]
    db2.close()


# ------------------------------------------------------------------ A7

def test_a7_refresh_stats_skips_when_hidden():
    blk = _block(MW_SRC, "refresh_stats")
    assert "isHidden" in blk, "隐藏即跳过的逻辑不在 refresh_stats 里"
    assert "return" in blk


def test_a7_check_missing_uses_existing_paths_and_bg_thread():
    blk = _block(MW_SRC, "check_missing_on_start")
    assert "_existing_paths" in blk, "启动扫盘没复用 stats 的优化版"
    assert "_bg_submit" in blk, "扫盘没后置到后台线程"


# ------------------------------------------------------------------ A8

def test_a8_url_dialog_buttons_connect_exactly_once():
    """b_add/b_use 曾各连两次 ⇒ 点一次加两条、持久化两遍。"""
    assert MW_SRC.count("b_add.clicked.connect(_add_url)") == 1
    assert MW_SRC.count("b_use.clicked.connect(_use_url)") == 1
    # 死件 b_add2/b_use2 不再出现（注释里的历史说明剥掉再判）
    code = _code_only(MW_SRC)
    assert "b_add2" not in code and "b_use2" not in code


# ------------------------------------------------------------------ A9

def test_a9_edit_deleted_task_says_not_exists():
    """get_task=None 曾被当新建：用户以为在编辑，存出来却是个新任务。

    ⚠ 「新建任务」按钮本来就要 _open_task_form(None)（合法），所以不断言
    全文件没有它，只断言：编辑三兄弟块内没有、且都走「任务不存在」提示。
    """
    for name in ("_edit_by_id", "_edit_selected", "_open_task_form_by_item"):
        blk = _code_only(_block(MW_SRC, name))
        assert "任务不存在" in blk, f"{name} 没有「任务不存在」提示"
        assert "self.refresh()" in blk, f"{name} 忘了刷新列表"
        assert "_open_task_form(None)" not in blk


# ------------------------------------------------------------------ B4

def test_b4_clean_scan_dirs_whitelist(tmp_path):
    """0 字节/.part 清理只扫：软件三个目录 + 下载任务的 output_dir 子目录；
    learn_folder 任务（entry_urls 是本地路径）的 output_dir 整棵排除——
    那是用户自选文件夹，扫它就是清用户相册。"""
    lib, iso, rev = (tmp_path / n for n in ("library", "iso", "review"))
    for d in (lib, iso, rev):
        d.mkdir()
    album = tmp_path / "用户相册"
    (album / "sub").mkdir(parents=True)
    dl = tmp_path / "下载"
    (dl / "site").mkdir(parents=True)

    db = Database(tmp_path / "mrm.db")
    _mk_task(db, "learn", ["/c/用户相册"], album)          # 本地路径任务
    _mk_task(db, "dl", ["https://x.com/a"], dl)            # 下载任务

    cfg = SimpleNamespace(library_dir=lib, isolate_dir=iso, review_dir=rev)
    dirs = clean_scan_dirs(cfg, db)
    resolved = {Path(d).resolve() for d in dirs}

    assert lib.resolve() in resolved
    assert iso.resolve() in resolved
    assert rev.resolve() in resolved
    assert (dl / "site").resolve() in resolved, "下载任务的落盘子目录要扫"
    assert album.resolve() not in resolved, "learn_folder 的 output_dir 被扫了"
    for r in resolved:
        assert album.resolve() not in r.parents, \
            f"清理范围越界进了用户相册: {r}"

    # 不传 db 时只扫软件自己的三个目录
    only = {Path(d).resolve() for d in clean_scan_dirs(cfg, None)}
    assert only == {lib.resolve(), iso.resolve(), rev.resolve()}


# ------------------------------------------------------------------ E1

def test_e1_handler_timeout_is_30():
    assert webui._Handler.timeout == 30


def test_e1_server_has_thread_gate_and_503_when_full():
    """满载时不挂线程：礼貌回 503 并关连接（idle 连接曾能把线程堆到上千）。"""
    srv = webui._WebHTTPServer(("127.0.0.1", 0), webui._Handler)
    try:
        assert srv.max_threads == 64
        assert isinstance(srv._slots, threading.BoundedSemaphore)
        sent, closed = [], []
        fake = SimpleNamespace(
            sendall=lambda b: sent.append(b), close=lambda: closed.append(1))
        srv._slots = threading.BoundedSemaphore(0)      # 伪造满载
        srv.process_request(fake, ("127.0.0.1", 1234))
        assert sent and b"503" in sent[0]
        assert closed == [1]
    finally:
        srv.server_close()


def test_e1_start_uses_gated_server():
    assert "_WebHTTPServer((host, self.port), _Handler)" in WEBUI_SRC


# ------------------------------------------------------------------ E2

class _BodyStub(webui._Handler):
    def __init__(self, payload, clen):
        self.rfile = io.BytesIO(payload)
        self._headers = {"Content-Length": str(clen)}
        self.close_connection = True

    @property
    def headers(self):
        return self._headers


def test_e2_oversize_body_drains_and_closes():
    """超 1MB：剩余 body 必须读干并断连——残留字节会被下一条请求当成
    请求行解析（keep-alive 协议错乱）。"""
    payload = b"x" * (1024 * 1024 + 128)
    h = _BodyStub(payload, len(payload))
    assert h._body() == {}
    assert h.close_connection is True
    assert h.rfile.read() == b"", "body 没读干，残留字节会污染下一条请求"


def test_e2_normal_body_still_parses():
    h = _BodyStub(b'{"a": 1}', 8)
    assert h._body() == {"a": 1}
    assert h.close_connection is True      # 初始值，未被 _body 关连接


def test_e2_no_content_length_returns_empty():
    h = _BodyStub(b"", 0)
    assert h._body() == {}


# ------------------------------------------------------------------ E4

class _SendStub(webui._Handler):
    def __init__(self):
        self.caps = []
        self.wfile = io.BytesIO()

    def send_response(self, code, message=None):
        self.caps.append((":status", str(code)))

    def send_header(self, k, v):
        self.caps.append((k, v))

    def end_headers(self):
        pass


def test_e4_security_headers_are_sent():
    h = _SendStub()
    h._send(200, b"ok", "text/plain; charset=utf-8")
    caps = dict(h.caps)
    assert caps.get("X-Content-Type-Options") == "nosniff"
    # 2026-10-27（编号 28）：默认从 SAMEORIGIN 收紧成 DENY。
    # 理由：本软件没有任何"被别的页面嵌进去"的正当用途，而点击劫持正是
    # 用 iframe 套一层透明层骗点击。开关（web_frame_deny）关掉才回 SAMEORIGIN。
    assert caps.get("X-Frame-Options") in ("DENY", "SAMEORIGIN"), caps
    assert caps.get("X-Frame-Options") == "DENY", (
        "默认必须是 DENY（web_frame_deny 默认开）")
    assert caps.get("Referrer-Policy") == "no-referrer"
    csp = caps.get("Content-Security-Policy", "")
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp, "CSP 的 frame-ancestors 要跟 XFO 一致"
    assert h.wfile.getvalue() == b"ok"


def test_e4_headers_also_on_json_path():
    """API 响应同样带头（登录/批量接口最容易被钻）。"""
    h = _SendStub()
    h._json({"ok": True})
    caps = dict(h.caps)
    assert caps.get("X-Content-Type-Options") == "nosniff"
    assert "Content-Security-Policy" in caps
