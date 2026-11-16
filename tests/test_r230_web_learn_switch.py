"""R230 · Web 端「学习」开关护栏（R149-B 补全）。

订单出处：``.workbuddy/PARITY_MATRIX.md``「缺失项」第 3 条 —— 桌面端有
「学习」开关（R149-B：AI与过滤菜单 + AI 设置页），Web 端**只有代码层的
gate 生效**，用户看不见也勾不了。本轮把它补成两端对等。

分三层钉契约：

  L1 纯函数（``learn_switch`` —— 网页端**唯一**的读写入口）
    - 老配置无键 = 开；写开关 = 改 cfg + 同步全局 gate + 落盘，三步不分离

  L2 源码 / AST（防"功能被顺手删"）
    - GET / POST 两个路由都**接了**（同名路由必须连下一行一起判）
    - webui 自己**不许**直接赋值 cfg.learn_enabled（只能走共享层）
    - 学习看板 payload 带上 enabled；前端有勾选框 + 写回 + 失败回滚

  L3 行为（真 handler + 真库）
    - 读开关取 cfg；写开关真的落盘（save_config 被调用）且 gate 同步
    - 缺参数给**中文**报错，不 500、不静默

变异自检见 ``.workbuddy/_r230_mutate.py``（每条判据都要能红）。
"""
import ast
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from media_resource_manager.classify.preference import (      # noqa: E402
    is_learning_enabled,
    set_learning_enabled,
)
from media_resource_manager.config import AppConfig           # noqa: E402
from media_resource_manager.db import Database                # noqa: E402
from media_resource_manager import learn_switch               # noqa: E402

WEBUI_PY = ROOT / "media_resource_manager" / "webui.py"
APP_JS = ROOT / "media_resource_manager" / "web" / "static" / "js" / "app.js"
LEARN_SWITCH_PY = ROOT / "media_resource_manager" / "learn_switch.py"

ROUTE = "/api/learn/enabled"


@pytest.fixture(autouse=True)
def _reset_gate():
    """每个用例后把全局 gate 复位，避免污染其它套件（test_r149 同款）。"""
    prev = is_learning_enabled()
    set_learning_enabled(True)
    yield
    set_learning_enabled(prev)


def _lines(path):
    return path.read_text(encoding="utf-8").replace("\r\n", "\n").split("\n")


def _route_wired(verb_call_fragment):
    """同名路由 GET / POST 是**同一行字面量** ⇒ 必须连下一行一起判。

    R228 踩过：只判 ``if path == "/api/learn/enabled":`` 的话，把 POST 的实现
    换掉（等于没接路由）照样绿。
    """
    ls = _lines(WEBUI_PY)
    for i, ln in enumerate(ls):
        if ln.strip() == 'if path == "%s":' % ROUTE:
            nxt = ls[i + 1] if i + 1 < len(ls) else ""
            if verb_call_fragment in nxt:
                return True
    return False


def _webui_assigns_learn_enabled():
    """AST 扫：webui 里有没有**直接赋值** cfg.learn_enabled。

    不看字符串（注释里出现同名字样会假红），只看真实赋值语句。
    """
    tree = ast.parse(WEBUI_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        for t in targets:
            if isinstance(t, ast.Attribute) and t.attr == learn_switch.FIELD:
                return True
    return False


def _handler(db=None, cfg=None):
    from media_resource_manager import webui

    class _H(webui._Handler):
        def __init__(self, d, c):
            self._db = d
            self._cfg = c
            self.headers = {}
            self.client_address = ("127.0.0.1", 0)

        @property
        def db(self):
            return self._db

        @property
        def cfg(self):
            return self._cfg

    return _H(db, cfg)


def _mk_db(tmp_path):
    """C10 之后 images.task_id 仍 NOT NULL ⇒ 图必须挂在真任务上。"""
    db = Database(tmp_path / "r230.db")
    cur = db.execute("INSERT INTO tasks(name) VALUES (?)", ("R230",))
    db._tid = int(cur.lastrowid)
    return db


# ═══════════════════════ L1 · 纯函数 ═══════════════════════

def test_read_enabled_defaults_on_for_legacy_config():
    class _Old:            # 老配置根本没有 learn_enabled 这个键
        pass

    assert learn_switch.read_enabled(_Old()) is True
    cfg = AppConfig()
    cfg.learn_enabled = False
    assert learn_switch.read_enabled(cfg) is False


def test_apply_enabled_sets_field_and_gate():
    cfg = AppConfig()
    assert learn_switch.apply_enabled(cfg, False) is False
    assert cfg.learn_enabled is False
    assert is_learning_enabled() is False
    assert learn_switch.apply_enabled(cfg, True) is True
    assert is_learning_enabled() is True


def test_apply_enabled_persists_exactly_once():
    cfg = AppConfig()
    calls = []
    learn_switch.apply_enabled(cfg, False, save=lambda: calls.append(1))
    assert calls == [1], "落盘只能一次，多写一次就是多余的磁盘 IO"


def test_apply_enabled_without_save_never_reaches_disk_layer():
    """共享层**不许**自己 import 落盘函数 —— 两端落盘链不同，只能参数注入。

    （写死 config.save_config 的话，桌面端那条保存链就被绕过去了：只改内存，
    重启丢设置，用户会以为「我关了它又自己开了」。）
    """
    src = LEARN_SWITCH_PY.read_text(encoding="utf-8")
    # ⚠ 只查**真实 import**：docstring 里也写了 save_config 这个名字
    #   （R73/R229 同族：判据被注释/docstring 骗到），全文搜会假红。
    tree = ast.parse(src)
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            imported.update(a.name for a in n.names)
        elif isinstance(n, ast.Import):
            imported.update(a.name.split(".")[0] for a in n.names)
    assert "save_config" not in imported, \
        "共享层不许直接 import 落盘函数，save 必须是调用方注入"
    cfg = AppConfig()
    learn_switch.apply_enabled(cfg, False)          # 不传 save
    assert cfg.learn_enabled is False               # 内存态照样改
    assert is_learning_enabled() is False


def test_apply_enabled_coerces_to_bool():
    cfg = AppConfig()
    assert learn_switch.apply_enabled(cfg, "yes") is True
    assert learn_switch.apply_enabled(cfg, 0) is False


def test_shared_layer_is_the_only_implementation():
    """共享层的字段名与 config 默认值必须同源，不许两份默认值各写各的。"""
    assert learn_switch.DEFAULT is True
    assert AppConfig().learn_enabled == learn_switch.DEFAULT
    src = LEARN_SWITCH_PY.read_text(encoding="utf-8")
    assert "from .classify.preference import set_learning_enabled" in src


# ═══════════════════════ L2 · 源码 / AST ═══════════════════════

def test_get_route_is_wired():
    assert _route_wired("_api_learn_enabled()"), \
        "GET 路由没接：%s" % ROUTE


def test_post_route_is_wired():
    assert _route_wired("_api_learn_enabled_set("), \
        "POST 路由没接：%s" % ROUTE


def test_webui_never_assigns_the_field_directly():
    assert _webui_assigns_learn_enabled() is False, \
        "webui 直接改 cfg.learn_enabled 了 ⇒ 与桌面端两份实现，迟早漂"
    src = WEBUI_PY.read_text(encoding="utf-8")
    assert "from .learn_switch import apply_enabled" in src


def test_learning_payload_carries_enabled():
    src = WEBUI_PY.read_text(encoding="utf-8")
    assert '"enabled": read_enabled(self.cfg)' in src, \
        "学习看板 payload 没带 enabled ⇒ 前端要多打一次请求"

def test_api_learn_enabled_reads_cfg():
    cfg = AppConfig()
    cfg.learn_enabled = False
    h = _handler(cfg=cfg)
    out = h._api_learn_enabled()
    assert out["ok"] is True and out["enabled"] is False
    cfg.learn_enabled = True
    assert h._api_learn_enabled()["enabled"] is True


def test_api_learn_enabled_set_writes_persists_and_syncs_gate(monkeypatch):
    import media_resource_manager.config as CFG

    calls = []
    monkeypatch.setattr(CFG, "save_config", lambda c: calls.append(c))
    cfg = AppConfig()
    h = _handler(cfg=cfg)
    out = h._api_learn_enabled_set({"enabled": False})
    assert out["ok"] is True and out["enabled"] is False
    assert cfg.learn_enabled is False
    assert is_learning_enabled() is False, "gate 没同步 ⇒ 关了也照样学"
    assert len(calls) == 1, "必须落盘一次，否则重启就回到旧值"

    out = h._api_learn_enabled_set({"enabled": True})
    assert out["enabled"] is True and is_learning_enabled() is True


def test_api_learn_enabled_set_missing_param_is_chinese_error():
    cfg = AppConfig()
    h = _handler(cfg=cfg)
    out = h._api_learn_enabled_set({})
    assert out["ok"] is False
    err = str(out.get("error") or "")
    assert err, "缺参数必须给提示，不能静默成功"
    assert any("一" <= ch <= "鿿" for ch in err), \
        "用户可见错误必须中文：%r" % err
    assert cfg.learn_enabled is True, "报错时不该把原来的开关改掉"


def test_learning_payload_enabled_follows_cfg(tmp_path):
    db = _mk_db(tmp_path)
    cfg = AppConfig()
    cfg.learn_enabled = False
    h = _handler(db=db, cfg=cfg)
    out = h._api_learning()
    assert out["ok"] is True
    assert out["enabled"] is False, "看板 payload 的 enabled 没跟 cfg 走"
