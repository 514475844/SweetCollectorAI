"""r36：锁死两个「用户点一下就出事」的 BUG。

1) 复制任务漏字段（**该功能已于 2026-10-07 随远程需求整体下线**）
   `_clone_task` 以前没带上 r29 的主题词分级三个字段（ai_labels_l2 /
   ai_labels_l3 / ai_label_strict），也没带 max_pages / keyword_subdir。
   复制＝「一模一样的再来一条」，漏了就等于副本跟原任务行为不同：
   二三级词空 → 不再细分主题词、strict 从开变关 → 该隔离的图不隔离了。

   现在菜单入口（复制任务 / 存为模板 / 从模板新建）与四个方法一起撤掉了，
   所以下面那条测试**从正向「不漏字段」改成了反向「不许复活」**——
   功能真撤掉后正向断言只会永远 AttributeError（假失败）。

2) refresh 无条件重建整张图库 → 段错误
   refresh() 尾巴里以前无条件 `self.load_gallery()`，哪怕图库页根本不可见。
   图库记录一多（当前库 1882 行），每次刷新任务列表（点复制任务 / 停止 /
   任务状态变化）都把整张图库重画一遍，实测直接把进程打成 SIGSEGV：
   C 层段错误、gui-crash.log 一片空白（段错误不写 Python 异常），
   用户侧表现就是「点一下复制任务，软件悄悄没了」。
"""
import os

import pytest

from media_resource_manager.models import TaskConfig


def _boot(monkeypatch, tmp_path):
    """起一个离屏 MainWindow（照 tests/test_task_max_pages.py 的套路）。"""
    home = os.path.join(str(tmp_path), "mrm_home")
    os.makedirs(home, exist_ok=True)
    monkeypatch.setenv("MRM_HOME", home)
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("MRM_NO_MIGRATE", "1")
    from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
    app = QApplication.instance() or QApplication([])
    import media_resource_manager.gui.main_window as mw
    mw._ensure_single_instance = lambda a: True
    for old in app.topLevelWidgets():
        try:
            from PySide6.QtCore import QTimer
            for tmr in old.findChildren(QTimer):
                tmr.stop()
            old.close()
        except Exception:
            pass
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    QMessageBox.warning = staticmethod(lambda *a, **k: None)
    QMessageBox.critical = staticmethod(lambda *a, **k: None)
    QDialog.exec = lambda self: QDialog.DialogCode.Accepted
    from media_resource_manager.gui import theme
    theme.apply_theme(app, False)
    win = mw.MainWindow()
    win.show()
    app.processEvents()
    win._keep_panel = win._build_task_form_panel()
    return app, win


def _full_task(name="模板站"):
    """一条把所有业务字段都填满的任务（命中过漏字段的那些）。"""
    return TaskConfig(
        id=None, name=name, adapter_name="pictoa",
        entry_urls=["https://www.pictoa.com/albums/1/"],
        keywords=["关键词"], blacklist=["排除词"],
        proxy="http://127.0.0.1:10808", proxy_id=7, use_proxy=True,
        concurrency=6, max_depth=4, delay=1.5,
        output_dir=r"E:/PIC", auto_run=False,
        priority=3,
        ai_enabled=True, ai_labels=["NSFW"], ai_blacklist=["马赛克"],
        ai_threshold=0.35, ai_match_mode="all",
        ai_exclude_match_mode="all",
        ai_labels_l2=["丝袜", "袜"],
        ai_labels_l3=["白丝", "黑丝", "船袜"],
        ai_label_strict=True,
        min_width=300, min_height=400, min_bytes=1024, max_bytes=9_000_000,
        schedule_at="08:30", rate_limit=60, allowed_hours="22-6",
        max_pages=5, keyword_subdir=True,
        cookie="cf_clearance=abc")


@pytest.fixture()
def win_env(tmp_path, monkeypatch):
    app, win = _boot(monkeypatch, tmp_path)
    yield win
    try:
        win.close()
    except Exception:
        pass


def test_clone_task_back_with_menu(win_env):
    """【R154-F16 → **R170 撤回克隆** · 2026-10-20 用户拍板】模板保留、克隆移除。

    历史：2026-10-07 远程需求把「复制任务/存为模板/从模板新建」整体下线
    （反向护栏）；2026-10-17 R154-F16 重新上线「克隆 + 存为模板」；
    **2026-10-20 R170 用户拍板再撤「克隆」** —— 理由：克隆后容易忘改配置，
    两个相同任务同时跑=重复采集（用户原话担忧）。「存为模板 / 用模板新建」
    **保留**（模板不自动跑）。

    判据（沿用「菜单与能力必须同步」精神，方向随订单反转）：
    ① 克隆的**用户入口必须消失**：_clone_task 方法、右键菜单项、
       Web 按钮与接口，一个都不许留；
    ② 模板能力**必须还在**：表单模板三件套 + 存为模板菜单项 + 库层字段；
    ③ 库层 `taskio.clone_task` **保留**（模板新建依赖它，删了会打死模板）；
    ④ 旧下线交互的具体标签仍不许复活。
    """
    # ① 克隆入口全消失
    #    ⚠ 判据是**可执行入口**（menu.addAction / 方法定义 / API 路径），
    #    不是「克隆任务」这四个字 —— 源码里保留「已移除」的说明注释是对的，
    #    按字符串判会把解释性注释也当成"功能还在"（假红）。
    assert not hasattr(win_env, "_clone_task"), \
        "_clone_task 回来了 —— R170 已按用户拍板删除克隆功能"
    assert hasattr(win_env, "_save_task_as_template"), \
        "存为模板方法不见了 —— R170 只删克隆，模板要保留"
    src_path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "media_resource_manager", "gui",
        "main_window.py")
    with open(src_path, encoding="utf-8") as f:
        src = f.read()
    assert 'menu.addAction("克隆任务"' not in src, \
        "菜单项「克隆任务」还在 —— R170 已删除"
    assert "def _clone_task(" not in src, \
        "_clone_task 方法还在 —— R170 已删除"
    # ② 模板能力保留
    assert "存为模板…" in src, "菜单项「存为模板…」不见了（R170 只删克隆）"
    for lit in ("self.f_tpl = QComboBox()",
                "b_tpl_apply.clicked.connect(self._apply_template)",
                "b_tpl_save.clicked.connect(self._save_form_as_template)"):
        assert lit in src, "表单模板控件「%s」被删了（R170 不该动模板）" % lit
    # ③ 库层克隆能力保留给模板用
    from media_resource_manager import taskio
    assert hasattr(taskio, "clone_task"), \
        "taskio.clone_task 被删了 —— 模板新建会一起坏掉"
    # ④ 旧下线交互的具体标签不许复活
    for label in ("复制任务（克隆配置）", "从模板新建"):
        assert label not in src, \
            "菜单项「%s」是 2026-10-07 已下线的旧交互，不该复活" % label
    # ⑤ 库层字段必须还在：模板行要能存/读，不能因为 GUI 上下线把列删了
    from media_resource_manager.models import TaskConfig
    assert "is_template" in TaskConfig.__dataclass_fields__, \
        "is_template 字段被删了（模板行会读不出来）"
    assert "parent_id" in TaskConfig.__dataclass_fields__, \
        "parent_id 字段被删了（历史任务行会读不出来）"



def test_refresh_does_not_rebuild_hidden_gallery(win_env, monkeypatch):
    """图库页不可见时，refresh 不能顺手把整张图库重画一遍。"""
    calls = []
    monkeypatch.setattr(win_env, "load_gallery",
                        lambda *a, **k: calls.append(1))
    win_env.refresh()
    assert calls == [], "图库页不可见却在 refresh 里重建图库（会拖垮/崩）"

    # 切到图库页要自己加载一次（不然图库页会是空的）
    win_env.stack.setCurrentIndex(win_env.stack.indexOf(win_env._gallery_tab))
    win_env._nav_go(win_env.stack.indexOf(win_env._gallery_tab))
    assert len(calls) >= 1, "切到图库页没有加载图库（图库会是空的）"


def test_nav_to_gallery_reloads(win_env, monkeypatch):
    """切页加载这条路径单独锁一次：别被上面的守卫把图库卡死。"""
    calls = []
    monkeypatch.setattr(win_env, "load_gallery",
                        lambda *a, **k: calls.append(1))
    gt = win_env._gallery_tab
    win_env._nav_go(win_env.stack.indexOf(gt))
    assert calls, "切到图库页没加载"
