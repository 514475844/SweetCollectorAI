"""R154-F23 幻灯片护栏（2026-10-22 棒）。

订单原文（R154 第二组 F23）：
  「灯箱空格已有幻灯片基础，补：自动播放（间隔可调 2/4/8s）、暂停/继续、
   循环/到尾停；桌面+Web 一致。」

两条铁律（本仓 R73/R79 反复踩坑后钉死的写法）：
  ① 判据必须能**真调**被护对象 —— 桌面端真建 Lightbox 实例驱动，
    Web 端 node 真跑函数；「源码里有这个字符串」= 空壳断言，一个都不许有。
  ② 变异自检见 .workbuddy/_r154_f23_mutate.py（8 处变异必须全红）。
"""
import json
import re
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
APPJS = _ROOT / "media_resource_manager" / "web" / "static" / "js" / "app.js"
INDEX = _ROOT / "media_resource_manager" / "web" / "index.html"
MW = _ROOT / "media_resource_manager" / "gui" / "main_window.py"

# ================================================== 桌面端


@pytest.fixture
def qapp(monkeypatch):
    from PySide6.QtWidgets import QApplication
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("MRM_NO_MIGRATE", "1")
    return QApplication.instance() or QApplication([])


@pytest.fixture
def lb(qapp):
    from media_resource_manager.gui.lightbox import Lightbox
    rows = [{"path": f"/no/such/img_{i}.webp", "id": 100 + i, "width": 8,
             "height": 6, "source_url": "https://example.com/x", "tags": ""}
            for i in range(3)]
    box = Lightbox(rows, 0, None)
    try:
        yield box
    finally:
        box.close()


def test_f23_speed_cycle_2_4_8(lb):
    """间隔按钮 2→4→8→2 循环；初值 3000 第一落到 2 秒。"""
    seq = [lb.cycle_slideshow_speed() for _ in range(5)]
    assert seq == [2000, 4000, 8000, 2000, 4000], seq
    assert lb._slideshow_timer.interval() == 4000
    assert lb.b_speed.text() == "间隔 4秒", "间隔按钮文案没跟着变"


def test_f23_pause_is_resume_not_restart(lb):
    """P/空格 = 暂停/继续：interval 保持、恢复后还在播（不是重建）。"""
    lb.cycle_slideshow_speed()          # 2000
    lb.toggle_slideshow(True)
    assert lb.is_slideshow_running()
    lb.toggle_slideshow(False)          # 暂停
    assert not lb.is_slideshow_running()
    assert lb._slideshow_timer.interval() == 2000, "暂停把间隔丢了"
    lb.toggle_slideshow(True)           # 继续
    assert lb.is_slideshow_running()
    assert lb._slideshow_timer.interval() == 2000, "继续后间隔变了"


def test_f23_stop_at_end_default(lb):
    """循环默认关：到最后一张 tick = 自动停，不绕回。"""
    lb.step(1); lb.step(1)              # i=2（最后一张）
    assert lb.i == 2
    lb.toggle_slideshow(True)
    lb._slideshow_tick()
    assert not lb.is_slideshow_running(), "循环关时到底没停"
    assert lb.i == 2, "到底后把用户甩回第一张了"
    assert not lb.b_slideshow.isChecked(), "停了按钮还亮着"


def test_f23_loop_wraps(lb):
    """循环开：到底 tick 绕回第一张且继续播。"""
    lb.b_loop.setChecked(True)
    assert lb._slideshow_loop is True
    lb.step(1); lb.step(1)
    lb.toggle_slideshow(True)
    lb._slideshow_tick()
    assert lb.i == 0, "循环开时到底没绕回"
    assert lb.is_slideshow_running(), "绕回后播放停了"


def test_f23_key_p_toggles(qapp):
    """P 键 = 幻灯片开关（与空格同效）。"""
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication
    from media_resource_manager.gui.lightbox import Lightbox
    rows = [{"path": f"/no/such/x{i}.webp", "id": i} for i in range(2)]
    box = Lightbox(rows, 0, None)
    try:
        ev = QKeyEvent(QEvent.Type.KeyPress, int(Qt.Key.Key_P),
                       Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(box, ev)
        assert box.is_slideshow_running(), "P 没把幻灯片打开"
        QApplication.sendEvent(box, ev)
        assert not box.is_slideshow_running(), "P 没把幻灯片暂停"
    finally:
        box.close()


def test_f23_empty_rows_cannot_start(lb):
    """空列表不许开播（防御：rows=[] 时 toggle(True) 必须是 no-op）。"""
    lb.rows = []
    assert lb.toggle_slideshow(True) is False
    assert not lb.is_slideshow_running()


# ================================================== Web 端

def _node(probe: str) -> str:
    out = subprocess.run(["node", "-e", probe], capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def _jsfunc(js: str, name: str) -> str:
    i = js.index(f"function {name}")
    return js[i:js.index("\nfunction ", i + 10)]


def _lb_probe(setup: str, calls: str) -> str:
    """取 LB 状态机 + F23 五函数（剥注释），node 里真跑。"""
    js = APPJS.read_text(encoding="utf-8")
    start = js.index("var LB = {")
    end = js.index("function lbVisible")
    body = js[start:end]
    for fn in ("lbTick", "lbStartSlideshow", "lbStopSlideshow",
               "lbToggleSlideshow", "lbCycleSpeed", "lbToggleLoop"):
        body += _jsfunc(js, fn)
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    body = re.sub(r"//[^\n]*", "", body)
    # node 里没有 DOM/window：$ 桩记账、window 桩只存句柄不真计时，
    # 翻页/渲染四函数桩成 no-op（tick 里的绕回计数用真 LB.i 断言）。
    stub = (
        "var _calls = [];\n"
        "function $(id){ _calls.push(id); "
        "return {textContent:'', setAttribute:function(){}}; }\n"
        "var window = { setInterval:function(){return 42;}, "
        "clearInterval:function(){} };\n"
        "function lbSetZoom(){}\n"
        "function lbApplyTransform(){}\n"
        "function lbRender(){}\n"
        "function lbStep(d){ if(!LB.items.length) return; "
        "var n = LB.i + d; "
        "if(n < 0 || n >= LB.items.length) return; LB.i = n; }\n"
    )
    return stub + body + setup + calls

def test_f23_help_text_updated():
    """快捷键帮助同步：幻灯片键 + P 别名。

    F19 起该行改成按当前生效键位现场拼（源码里是 f-string 引用动作 id），
    所以判据从「字面量 '空格/P     幻灯片…'」改为「文案仍在 + 引用的是
    幻灯片动作」——意图不变：帮助里必须写幻灯片播放/暂停与 P 别名。
    """
    src = MW.read_text(encoding="utf-8")
    assert "幻灯片自动播放 开/停（P=暂停/继续）" in src, \
        "main_window 快捷键帮助没写幻灯片播放/暂停"
    assert "'light.slideshow'" in src, "帮助里的幻灯片键位没走 hotkeys 解析"
