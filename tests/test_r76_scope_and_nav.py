"""r76：需求 9（删「日志统计」导航项）+ 需求 16（「不喜欢」二级菜单选范围）的锁。

两层断言：
- **AST 层**：判「可达性/调用关系」，不判字面量（本仓注释里大量 quoting 需求原文，
  全文搜字面量会匹配到注释自己 ⇒ 假绿，R63-D3/R73 复发过两次）。
- **行为层**：直接调产品方法（`MainWindow._add_exclusions` / `Engine._blacklist_rules`
  用 unbound 方式挂到桩对象上跑），真写库、真落 config，不靠"看起来对"。

需求 16 四条硬约束全部有对应断言：
1. 作用范围真的可选（task / global）；
2. **主题词永不进排除词**（两种 scope 都拦）；
3. **全局黑名单真的被 engine 并上**（否则"全局"是个假开关 = 做了另一件事）；
4. **去重幂等**（重复调用不把列表撑大）。
"""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MW = ROOT / "media_resource_manager" / "gui" / "main_window.py"
RT = ROOT / "media_resource_manager" / "gui" / "review_tab.py"
ENG = ROOT / "media_resource_manager" / "engine.py"
CFG = ROOT / "media_resource_manager" / "config.py"


def _tree(p: Path):
    return ast.parse(p.read_bytes().decode("utf-8").replace("\r\n", "\n"))


def _calls(node, attr: str):
    """通用递归收集所有 `X.<attr>(...)` 调用（不只认 If/Try，R69 教训）。"""
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr == attr:
            out.append(n)
    return out


# ===================================================================== #
# 需求 9：底部「日志统计」导航项已撤，页面还在
# ===================================================================== #
def test_bottom_tabbar_hidden():
    """导航项（页签条）被撤。用 isHidden 语义的那一边：setVisible(False)。"""
    # 形态是 self.bottom_tabs.tabBar().setVisible(False)
    # ⇒ func.value 是**一次 Call**（tabBar()），不是 Attribute
    hits = [c for c in _calls(_tree(MW), "setVisible")
            if isinstance(c.func.value, ast.Call)
            and isinstance(c.func.value.func, ast.Attribute)
            and c.func.value.func.attr == "tabBar"
            and c.args and isinstance(c.args[0], ast.Constant)
            and c.args[0].value is False]
    assert hits, "底部页签条没有 setVisible(False) —— 需求 9 未实现"


def test_bottom_page_still_added():
    """反向护栏：撤的是**页签条**，不是页面。

    漏掉 addTab 会让日志画在孤儿页上、界面底部一片空白（历史踩过）。
    """
    hits = [c for c in _calls(_tree(MW), "addTab")
            if c.args and isinstance(c.args[0], ast.Attribute)
            and c.args[0].attr == "_stats_tab"]
    assert len(hits) == 1, f"addTab(self._stats_tab, ...) 应恰好 1 处，实得 {len(hits)}"


def test_maintenance_log_menu_kept():
    """反向护栏：顶部「维护 → 日志统计」子菜单必须保留。

    verify_r11 把它的名字钉在 18/18 基线里；需求 9 说的是**导航项**，不是这个菜单。
    """
    hits = [c for c in _calls(_tree(MW), "addMenu")
            if c.args and isinstance(c.args[0], ast.Constant)
            and c.args[0].value == "日志统计"]
    assert hits, "维护菜单里的「日志统计」子菜单不能撤（verify_r11 基线）"


# ===================================================================== #
# 需求 16：菜单真的变成二级
# ===================================================================== #
@pytest.mark.parametrize("path,label", [(MW, "不喜欢（删除）"),
                                        (RT, "不喜欢")])
def test_dislike_is_submenu(path, label):
    # 图库那处是 addMenu(icons.icon(...), "不喜欢（删除）") —— 标签在 args[1]，
    # 不能只认 args[0]。
    hits = [c for c in _calls(_tree(path), "addMenu")
            if any(isinstance(a, ast.Constant) and a.value == label
                   for a in c.args)]
    assert hits, f"{path.name}: 「{label}」不是二级菜单（应走 addMenu）"


@pytest.mark.parametrize("path", [MW, RT])
def test_dislike_submenu_has_two_scopes(path):
    """二级菜单里必须同时有「仅当前任务」和「全局」两个范围项。"""
    src = path.read_bytes().decode("utf-8").replace("\r\n", "\n")
    tree = ast.parse(src)
    # 判「字符串常量出现在文件里」会被注释骗，这里判的是**真实常量节点**
    consts = {n.value for n in ast.walk(tree)
              if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert 'scope="task"' in src or "仅当前任务" in consts, \
        f"{path.name}: 缺「仅当前任务」范围项"
    assert "仅当前任务" in consts, f"{path.name}: 缺「仅当前任务」文案"
    assert any("全局" in c for c in consts), f"{path.name}: 缺「全局」范围项"


def test_no_flat_dislike_action_left():
    """反向：不能还留着旧的平铺「不喜欢」项（否则二级菜单是摆设）。"""
    src = MW.read_bytes().decode("utf-8").replace("\r\n", "\n")
    assert '"不喜欢（删除）", self.gallery_dislike)' not in src, \
        "图库还留着旧的平铺「不喜欢（删除）」项"


# ===================================================================== #
# 需求 16：全局黑名单真的被过滤链路并上（不是假开关）
# ===================================================================== #
def test_engine_uses_merged_blacklist():
    """三个过滤点都必须走 _blacklist_rules，不许再直接用 task.blacklist。"""
    tree = _tree(ENG)
    merged = _calls(tree, "_blacklist_rules")
    assert len(merged) >= 3, \
        f"engine 里 _blacklist_rules 调用点应 ≥3，实得 {len(merged)}"
    for c in _calls(tree, "_hit_blacklist"):
        args = {ast.unparse(a) for a in c.args}
        assert "task.blacklist" not in args, \
            "_hit_blacklist 还在直接用 task.blacklist（全局黑名单会被漏掉）"


def test_blacklist_rules_merges_global():
    """行为层：真调产品方法，全局黑名单并进结果。"""
    from media_resource_manager.engine import Engine

    class E:
        pass

    e = E()
    e.cfg = type("C", (), {"global_blacklist": ["全局词"]})()
    task = type("T", (), {"blacklist": ["任务词"]})()
    assert Engine._blacklist_rules(e, task) == ["任务词", "全局词"]


# ===================================================================== #
# 需求 16：config 层 —— 去重幂等 / 空词丢弃
# ===================================================================== #
def test_add_global_blacklist_idempotent(tmp_path, monkeypatch):
    from media_resource_manager.config import (AppConfig, add_global_blacklist)

    saved = []
    monkeypatch.setattr("media_resource_manager.config.save_config",
                        lambda cfg, path=None: saved.append(cfg))
    cfg = AppConfig(db_path=tmp_path / "a.db", library_dir=tmp_path / "lib",
                    last_output_dir=tmp_path / "lib",
                    rules_dir=tmp_path / "rules",
                    isolate_dir=tmp_path / "q",
                    review_dir=tmp_path / "r", log_dir=tmp_path / "lg")
    assert add_global_blacklist(cfg, ["猫"]) == ["猫"]
    assert add_global_blacklist(cfg, ["猫"]) == []          # 幂等：第二次返回空
    assert cfg.global_blacklist == ["猫"]                    # 没被撑大
    assert add_global_blacklist(cfg, ["  ", ""]) == []       # 空词丢弃
    assert add_global_blacklist(cfg, ["狗"]) == ["狗"]
    assert cfg.global_blacklist == ["猫", "狗"]
    assert len(saved) == 2, "只有真正新增时才落盘"


def test_appconfig_has_global_blacklist_default():
    from media_resource_manager.config import AppConfig
    import dataclasses
    names = {f.name for f in dataclasses.fields(AppConfig)}
    assert "global_blacklist" in names
    assert AppConfig().global_blacklist == [], "默认必须为空（老配置迁移后行为不变）"


# ===================================================================== #
# 需求 16：_add_exclusions 的两种 scope + 主题词护栏
# ===================================================================== #
def _stub(db, cfg):
    """挂产品方法的桩：只补方法真正用到的三个属性。"""
    class Sig:
        def __init__(self):
            self.msgs = []

        def emit(self, m):
            self.msgs.append(m)

    class S:
        pass

    s = S()
    s.db = db
    s.cfg = cfg
    s.log_signal = Sig()
    return s


def _mk_task(db, ai_labels, ai_blacklist):
    from media_resource_manager.models import TaskConfig
    from media_resource_manager.repo import save_task
    t = TaskConfig(id=None, name="t", adapter_name="generic")
    t.ai_labels = list(ai_labels)
    t.ai_blacklist = list(ai_blacklist)
    # save_task 在 insert 分支**返回**新 id（不会回写 t.id）
    return save_task(db, t)


def test_scope_task_writes_task_only(tmp_config):
    from media_resource_manager.db import Database
    from media_resource_manager.gui.main_window import MainWindow
    from media_resource_manager.repo import get_task

    db = Database(tmp_config.db_path)
    tid = _mk_task(db, ["金发"], [])
    stub = _stub(db, tmp_config)
    MainWindow._add_exclusions(stub, tid, ["黑白照"], scope="task")
    assert get_task(db, tid).ai_blacklist == ["黑白照"]
    assert list(getattr(tmp_config, "global_blacklist", []) or []) == [], \
        "scope=task 不该动全局黑名单"


def test_scope_global_writes_global_only(tmp_config):
    from media_resource_manager.db import Database
    from media_resource_manager.gui.main_window import MainWindow
    from media_resource_manager.repo import get_task

    db = Database(tmp_config.db_path)
    tid = _mk_task(db, ["金发"], [])
    stub = _stub(db, tmp_config)
    MainWindow._add_exclusions(stub, tid, ["黑白照"], scope="global")
    assert getattr(tmp_config, "global_blacklist", []) == ["黑白照"], \
        "scope=global 必须写进全局黑名单"
    assert get_task(db, tid).ai_blacklist == [], \
        "scope=global 不该重复写任务排除词（engine 已并全局）"


@pytest.mark.parametrize("scope", ["task", "global"])
def test_theme_word_never_into_exclusions(tmp_config, scope):
    """【硬性规则】主题词永不进排除词 —— 两种 scope 都必须拦住。"""
    from media_resource_manager.db import Database
    from media_resource_manager.gui.main_window import MainWindow
    from media_resource_manager.repo import get_task

    db = Database(tmp_config.db_path)
    tid = _mk_task(db, ["金发"], [])
    stub = _stub(db, tmp_config)
    MainWindow._add_exclusions(stub, tid, ["金发"], scope=scope)
    assert get_task(db, tid).ai_blacklist == [], \
        f"scope={scope}: 主题词被写进了任务排除词"
    assert list(getattr(tmp_config, "global_blacklist", []) or []) == [], \
        f"scope={scope}: 主题词被写进了全局黑名单"
