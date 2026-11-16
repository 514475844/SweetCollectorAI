"""r39b：AST 静态锁「类内把自身方法当全局函数裸调用」（缺 self. 前缀）。

真机现场（r39 前台真窗口按钮全扫描，扫描日志第一条 traceback 之后又抓一批）：

    File "media_resource_manager/gui/main_window.py", line 4828, in url_adapter_manager
        _reload_adapters()
    NameError: name '_reload_adapters' is not defined. Did you mean: 'self._reload_adapters'?

顺着这条线索扫全文件，又揪出 **8 处裸 refresh()**（隔离区「还原 / 彻底删除 /
清空隔离区 / 清理缺失记录」回调 + _purged 快照对话框的 restore / delete /
构建末尾）。这 8 处的共同点：它们都发生在弹窗里，前面的 ``db.execute`` 和
``self.log_signal.emit`` 已经跑完了 —— 所以**用户看到的是「日志说还原了 N 张」，
表格却纹丝不动，控制台狂刷 NameError**，光看界面根本发现不了。

这类错误编译期不报（Python 函数名是运行时解析的）、单测也几乎覆盖不到
（要刚好点到那个弹窗的那个按钮），只有真机点按钮才现形。所以用 AST 做一次
全模块扫描兜底：一个类里方法体内如果以裸 ``refresh()`` 这种 ``Name`` 形式调用
**同一个类里定义过的**方法名，就是 bug。
（注意：必须是「这个类自己定义过的方法名」，模块级 helper 同名不算。）
"""
import ast
import pathlib

import pytest

ROOT = pathlib.Path("media_resource_manager")
GLOB = "**/*.py"


def _py_files():
    return sorted(ROOT.rglob("*.py"))


def _bare_calls_in(path):
    """返回 [(class_name, method_name, lineno)]：类内裸调用自身方法。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits = []
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        own = {b.name for b in cls.body
               if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef))}
        if not own:
            continue
        for node in ast.walk(cls):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in own):
                hits.append((cls.name, node.func.id, node.lineno))
    return hits


def _all_bare():
    bad = []
    for p in _py_files():
        for hit in _bare_calls_in(p):
            bad.append((str(p).replace("\\", "/"),) + hit)
    return bad


def test_package_has_files():
    """防止 ROOT 路径写错导致「零命中」假绿。"""
    assert len(_py_files()) >= 20, "扫描到的源文件太少，路径可能写错了"


@pytest.mark.parametrize("rel", [str(p).replace("\\", "/")
                                 for p in _py_files()])
def test_no_bare_self_method_call(rel):
    """每个文件都不许出现「类内裸调用自身方法」。"""
    path = pathlib.Path(rel)
    hits = _bare_calls_in(path)
    assert hits == [], (
        "以下位置把类方法当全局函数裸调用了（应该写成 self.xxx()）：\n"
        + "\n".join(f"  {cls}.{m}()  line {ln}"
                    for cls, m, ln in sorted(hits))
    )


def test_known_fixed_sites_are_now_bound():
    """回归定位：隔离区 / _purged 快照那几处必须已经挂上 self.。"""
    src = (ROOT / "gui" / "main_window.py").read_text(encoding="utf-8")
    # 隔离区四个回调 + _purged 三个点，共 8 处
    assert src.count("        self.refresh()") >= 1
    assert "self._reload_adapters()\n" in src
    # 「刷新」按钮不能再把裸函数绑给 clicked（QPushButton 会传 bool 进去）
    assert '("刷新", lambda: self.refresh(), False)' in src
