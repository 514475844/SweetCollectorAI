"""V0.0.78 修复护栏：AutoVocab 轮询/收起/自动入库展示 + 桌面退出停 Web + 向导密码长度。

五条都是逐条复核过的真实 bug，每条一组回归测试：
  1. [AutoVocab.vue] 轮询单次失败不再放弃整个作业：catch 保留 jobId、不把
     running 置 false，继续定时 poll；连续失败 N 次才提示；
  2. [Learning.vue] 用 v-show 而非 v-if 挂载 AutoVocab，收起/展开不再卸载
     组件、清掉轮询定时器（实时进度与停止按钮得以保留）；
  3. [AutoVocab.vue] 自动入库数量（result.applied.added/updated）写进持久
     展示区，不再只靠 poll 的 message 一闪而过；
  4. [main_window.py] 显式退出 closeEvent 必须先停 _web_server 再关库，
     否则 serve_forever 线程继续查已关的库并遗留 socket；
  5. [onboarding.py] 向导里填写的 Web 密码要做 >=4 长度校验（占位文案承诺
     「至少 4 位」），太短不写入。

前端 1~3 用源码护栏（Vite 构建产物是哈希文件名，不钉产物）；Python 4~5
用「抽取方法源码 + 桩对象」做行为验证，不依赖真 GUI。
"""
from __future__ import annotations

import ast
import re
import textwrap
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _method_src(src: str, cls: str, name: str) -> str:
    """取类中某个方法的源码（去掉缩进），供行为桩直接 exec。"""
    tree = ast.parse(src)
    node = next(n for n in tree.body
                if isinstance(n, ast.ClassDef) and n.name == cls)
    fn = next(n for n in node.body
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == name)
    return textwrap.dedent(ast.get_source_segment(src, fn) or "")


# ====================================================================
# ① 轮询单次失败不放弃（AutoVocab.vue）
# ====================================================================

def test_autovocab_poll_catch_retries_and_keeps_job():
    s = _read("media_resource_manager/webapp/src/components/AutoVocab.vue")
    i = s.index("async function poll(id)")
    j = s.index("}, 900)", i)
    body = s[i:j]
    ci = body.index("} catch (e) {")
    catch = body[ci:]
    assert "poll(id)" in catch, \
        "catch 没重新发起 poll —— 一次网络抖动就中断了整个扫描作业"
    assert "running.value = false" not in catch, \
        "catch 把 running 置 false —— 开始按钮被误放开，可能发起第二个作业"
    assert "jobId.value = ''" not in catch, \
        "catch 清空了 jobId —— 旧作业结果/停止键会丢"
    assert "failStreak" in catch and "MAX_POLL_FAILS" in catch, \
        "缺连续失败计数与 N 次阈值：应连续失败 N 次才提示"


def test_autovocab_poll_success_resets_fail_streak():
    s = _read("media_resource_manager/webapp/src/components/AutoVocab.vue")
    i = s.index("async function poll(id)")
    j = s.index("}, 900)", i)
    body = s[i:j]
    # 成功路径（catch 之前）必须先清零，否则偶发失败会累积成误报
    pre = body[:body.index("} catch (e) {")]
    assert re.search(r"failStreak\s*=\s*0", pre), \
        "成功查询后没清零连续失败计数"
    assert "MAX_POLL_FAILS" in s and "const MAX_POLL_FAILS = 5" in s, \
        "缺连续失败 N 次才提示的阈值定义"


def test_autovocab_start_resets_poll_guards():
    s = _read("media_resource_manager/webapp/src/components/AutoVocab.vue")
    i = s.index("async function start()")
    j = s.index("// V0.0.76：随时停止", i)
    assert "failStreak = 0" in s[i:j], "新作业开始前没重置失败计数"


# ====================================================================
# ② 收起/展开不销毁组件（Learning.vue）
# ====================================================================

def test_learning_keeps_autovocab_mounted_with_vshow():
    s = _read("media_resource_manager/webapp/src/views/Learning.vue")
    assert '<AutoVocab v-show="showAutoVocab"' in s, \
        "AutoVocab 未用 v-show 保留实例：收起会卸载组件并清掉 pollTimer"
    assert '<AutoVocab v-if="showAutoVocab"' not in s, \
        "仍用 v-if 挂载 AutoVocab —— 老 bug 未修"
    assert 'showAutoVocab = !showAutoVocab' in s, "收起/展开开关被误删"


# ====================================================================
# ③ 自动入库数量持久展示（AutoVocab.vue）
# ====================================================================

def test_applied_count_written_to_persistent_display():
    s = _read("media_resource_manager/webapp/src/components/AutoVocab.vue")
    assert "const appliedNote = ref('')" in s, "缺持久展示 applied 的状态"
    assert "res.applied" in s, "结果里的 applied 统计没被前端读取"
    assert "appliedNote.value =" in s, "applied 统计没写进持久展示区"
    assert 'v-if="appliedNote && !running"' in s, \
        "applied 展示仍被绑在 running/pct 条件上，重开面板就消失"


# ====================================================================
# ④ 显式退出先停 Web 服务再关库（main_window.py）
# ====================================================================

def test_close_event_stops_web_server_before_db_close_source_order():
    s = _read("media_resource_manager/gui/main_window.py")
    body = _method_src(s, "MainWindow", "closeEvent")
    i_srv = body.index('getattr(self, "_web_server", None)')
    i_stop = body.index("srv.stop()", i_srv)
    i_db = body.index("self.db.close()")
    assert i_srv < i_stop < i_db, \
        "Web 服务的 stop 必须写在 self.db.close() 之前"


def test_close_event_behavior_stops_web_before_db():
    """行为验证：抽取 closeEvent 用桩跑一遍，确认 stop 先于 db.close。"""
    src = _read("media_resource_manager/gui/main_window.py")
    body = _method_src(src, "MainWindow", "closeEvent")
    # 零参 super() 依赖 __class__ 闭包，独立 exec 会报错；换成桩调用。
    body = body.replace("super().closeEvent(event)", "_super_close(event)")

    calls: list[str] = []

    class _Srv:
        def stop(self):
            calls.append("web_stop")

    class _DB:
        def close(self):
            calls.append("db_close")

    class _Queue:
        _thread = None

        def stop_all(self):
            calls.append("stop_all")

        def stop(self):
            calls.append("queue_stop")

    class _Pool:
        def waitForDone(self, ms=None):
            calls.append("thumb_wait")

    win = types.SimpleNamespace(
        _CLOSE_BUDGET_S=0.01,
        _queue=_Queue(),
        db=_DB(),
        _web_server=_Srv(),
        _thumb_pool=_Pool(),
        _bg_pool=None,
        _stop_thumb_pump=lambda *a, **k: calls.append("thumb_pump"),
        _save_ui_state=lambda *a, **k: calls.append("save_ui"),
        _join_threadpool=lambda pool, timeout=5.0: None,
        _wait_bg_qthreads=lambda budget=8.0: None,
        _stop_watch_folder=lambda: None,
    )
    ns = {
        "_tray_hide_on_close": lambda self_, ev: False,
        "_super_close": lambda ev: calls.append("super_close"),
    }
    exec(body, ns)
    ns["closeEvent"](win, object())

    assert "web_stop" in calls, "显式退出没有停 Web 服务"
    assert "db_close" in calls, "退出没关库"
    assert calls.index("web_stop") < calls.index("db_close"), \
        f"Web 服务 stop 晚于关库：{calls}"


# ====================================================================
# ⑤ 向导 Web 密码 >=4 校验（onboarding.py）
# ====================================================================

def test_onboarding_password_length_check_source_order():
    s = _read("media_resource_manager/gui/onboarding.py")
    body = _method_src(s, "WizardDialog", "apply_settings")
    assert "len(pw) < 4" in body, "向导缺 >=4 的密码长度校验"
    assert body.index("len(pw) < 4") < body.index("cfg.web_password = pw"), \
        "长度校验必须写在写入 cfg 之前"


@pytest.mark.parametrize("pw,should_save", [
    ("", False),      # 留空 = 免密，不写
    ("abc", False),   # <4 位：不写
    ("abcd", True),   # 恰好 4 位：写
    ("strong-pass", True),
])
def test_apply_settings_password_length_behavior(pw, should_save):
    """行为验证：抽取 apply_settings 用桩对象执行，确认短密码不落 cfg。"""
    src = _read("media_resource_manager/gui/onboarding.py")
    body = _method_src(src, "WizardDialog", "apply_settings")

    warned: list[tuple] = []

    class _MsgBox:
        @staticmethod
        def warning(*a, **k):
            warned.append((a, k))

    ns = {"QMessageBox": _MsgBox}
    exec(body, ns)

    class _Edit:
        def __init__(self, text=""):
            self._text = text

        def text(self):
            return self._text

    class _Combo:
        def currentData(self):
            return "builtin"

    fake = types.SimpleNamespace(
        backend_combo=_Combo(),
        model_path_edit=_Edit(),
        api_url_edit=_Edit(),
        api_key_edit=_Edit(),
        api_model_edit=_Edit(),
        web_password_edit=_Edit(pw),
    )
    cfg = types.SimpleNamespace()
    changed = ns["apply_settings"](fake, cfg)

    if should_save:
        assert changed.get("web_password") == pw, \
            f"合法密码应写入，实际 changed={changed}"
        assert getattr(cfg, "web_password") == pw
        assert warned == [], "合法密码不该弹「太短」警告"
    else:
        assert "web_password" not in changed, \
            f"密码 {pw!r} 太短却写进了 changed：{changed}"
        assert not hasattr(cfg, "web_password"), \
            f"密码 {pw!r} 太短却写进了 cfg"
        if pw:
            assert warned, "短密码被拦下但没给用户任何提示"
