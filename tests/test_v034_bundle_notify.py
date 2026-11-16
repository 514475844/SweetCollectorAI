"""V0.0.34 两件欠账：显卡推理组件随包分发 + 通知中心补齐后台事件。

判据尽量走关系/行为，不钉死版本号和整句文案 —— 上一轮吃过「钉旧值」
的亏（每次换皮、换文案都要重钉）。
"""
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "media_resource_manager"


def _read(rel: str) -> str:
    p = SRC / rel
    assert p.is_file(), f"缺文件 {rel}"
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------- 通知（行为）
def _fresh_db(tmp_path):
    from media_resource_manager.db import Database
    p = tmp_path / "mrm.db"
    d = Database(p)
    d.close()
    return p


def _rows(tmp_path):
    from media_resource_manager.db import Database
    d = Database(tmp_path / "mrm.db")
    try:
        return [dict(r) for r in d.query(
            "SELECT kind, level, title, body, link FROM notifications")]
    finally:
        d.close()


def _queue(tmp_path):
    from media_resource_manager.scheduler.queue import TaskQueue
    return TaskQueue(SimpleNamespace(db_path=tmp_path / "mrm.db"),
                     max_concurrent=1)


def test_terminal_queue_events_are_persisted(tmp_path):
    """任务跑完 / 出错 / 磁盘告警：人不在场也要能回头看到。"""
    _fresh_db(tmp_path)
    q = _queue(tmp_path)
    q.on_event("finished", 7, {"downloaded": 3, "elapsed": 1.2})
    q.on_event("low_disk", 7, "剩余 200MB")
    rows = _rows(tmp_path)
    kinds = {r["kind"] for r in rows}
    assert {"task-finished", "disk-low"} <= kinds, rows
    assert any("3 张" in r["body"] for r in rows), rows
    assert any(r["level"] == "error" for r in rows), rows


def test_process_events_do_not_flood_notifications(tmp_path):
    """running/queued/yielded 这类过程事件绝不落库，否则铃铛会被刷屏。"""
    _fresh_db(tmp_path)
    q = _queue(tmp_path)
    for ev in ("running", "queued", "yielded", "stopped", "resumed"):
        q.on_event(ev, 7, None)
    assert _rows(tmp_path) == []


def test_auth_error_emits_actionable_cookie_hint(tmp_path):
    """缺口之一：鉴权类报错要单独给「去检查 Cookie」这条能行动的建议。"""
    _fresh_db(tmp_path)
    q = _queue(tmp_path)
    q.on_event("error", 7, "403 Forbidden / login required")
    rows = _rows(tmp_path)
    hint = [r for r in rows if r["kind"] == "cookie-expired"]
    assert len(hint) == 1, rows
    assert hint[0]["level"] == "warn" and "/settings" in hint[0]["link"]
    assert any(r["kind"] == "task-error" for r in rows)


def test_on_event_callback_shape_is_preserved(tmp_path):
    """包装器不许改原有回调的调用形状（桌面端事件信号依赖它）。"""
    _fresh_db(tmp_path)
    seen = []

    def cb(*args, **kwargs):
        seen.append((args, kwargs))

    from media_resource_manager.scheduler.queue import TaskQueue
    q = TaskQueue(SimpleNamespace(db_path=tmp_path / "mrm.db"),
                  on_event=cb)
    q.on_event("resumed")
    q.on_event("finished", 7, {"downloaded": 1})
    assert seen[0][0] == ("resumed",), seen
    assert seen[1][0][0] == "finished"


def test_notify_write_failure_never_breaks_the_event(tmp_path):
    """通知写不进去时事件必须照样发得出去（notify.add 吞异常 + 包装器兜底）。"""
    from media_resource_manager.scheduler.queue import TaskQueue
    q = TaskQueue(SimpleNamespace(db_path=tmp_path / "no" / "such" / "mrm.db"))
    q.on_event("finished", 7, {"downloaded": 1})   # 不许抛


# ---------------------------------------------------------------- 通知（入口）
def test_backup_failure_paths_record_error_notification():
    w = _read("webui.py")
    assert w.count('"backup-failed"') >= 3, "备份失败三条路径都要留痕"
    assert "数据库备份失败" in w and "备份同步失败" in w


def test_import_job_reports_finish_and_failure():
    w = _read("webui.py")
    seg = w.split("def _api_import_folder(self, body):", 1)[1].split(
        "def _api_import_job", 1)[0]
    assert '"import-folder"' in seg
    for title in ("导入完成", "导入已停止", "导入失败"):
        assert f'_notice("{title}"' in seg, f"导入缺 {title} 通知"
    assert "/export?tab=imp" in seg, "通知链接要直达导入标签页"


# ---------------------------------------------------------------- 显卡组件随包
def test_spec_collects_dynamic_dlls_for_directml():
    spec = (ROOT / "build" / "pyinstaller.spec").read_text(encoding="utf-8")
    assert "collect_dynamic_libs" in spec and '"onnxruntime"' in spec, \
        "DirectML.dll 是运行时动态加载的，静态扫描抓不到，必须显式收"


def test_gpu_copy_no_longer_tells_user_to_install_runtime():
    vue = _read("webapp/src/views/Models.vue")
    lib = _read("models_lib.py")
    for t in (vue, lib):
        assert "安装 onnxruntime-directml" not in t
        assert "pypi.org/project/onnxruntime" not in t
    assert "已随软件内置" in vue and "已随软件内置" in lib
    assert "DirectX 12" in lib
