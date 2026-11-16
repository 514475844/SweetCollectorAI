"""V0.0.14 静态单测：``gui/subs_dialog.py`` 的字面量 / 纯函数 / 契约检查。

全程**不 import PySide6、不构造 QApplication、不启动事件循环**：只读源码文本，
用 ``ast`` 解析，并把模块里的纯函数抽出来在隔离命名空间里 ``exec`` 后验证行为。
因此本文件可单独运行，不会牵出 Qt，也不触碰任何现有模块的运行时状态。

词表里的口语词是「被检测的样本」，不是界面文案；护栏扫描的是产品码目录
（gui / webapp），不会把测试文件当界面。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "media_resource_manager" / "gui" / "subs_dialog.py"
TEXT = SRC.read_text(encoding="utf-8")
TREE = ast.parse(TEXT, filename=str(SRC))


def _imports():
    out = []
    for node in ast.walk(TREE):
        if isinstance(node, ast.ImportFrom):
            out.append((node.level, node.module,
                        [(a.name, a.asname) for a in node.names]))
    return out


# ---------------------------------------------------------------- 契约
def test_future_and_relative_imports():
    head = "\n".join(TEXT.splitlines()[:40])
    assert "from __future__ import annotations" in head
    assert any(level == 2 and module is None
               and ("subscribe", "sub") in aliases
               for level, module, aliases in _imports()), \
        "必须使用 from .. import subscribe as sub"
    assert any(level == 2 and module == "config"
               and ("save_config", None) in aliases
               for level, module, aliases in _imports()), \
        "必须从 ..config 导入 save_config"
    # 禁止单级相对导入：一个点会指向 gui 包内部并不存在的模块。
    for level, module, _ in _imports():
        assert not (level == 1 and module), "禁止 from .x import ..."


def test_dialog_signature_and_title():
    cls = next(n for n in TREE.body
               if isinstance(n, ast.ClassDef) and n.name == "SubsDialog")
    bases = [b.id for b in cls.bases if isinstance(b, ast.Name)]
    assert "QDialog" in bases
    init = next(n for n in cls.body
                if isinstance(n, ast.FunctionDef) and n.name == "__init__")
    assert [a.arg for a in init.args.args] == ["self", "db", "cfg", "parent"]
    assert len(init.args.defaults) == 1  # parent=None
    assert "站点订阅管理" in TEXT


def test_reuses_subscribe_functions_and_writes_no_sql():
    for name in ("list_subs", "get_sub", "save_sub", "delete_sub",
                 "snapshot_count", "preview", "check_sub"):
        assert f"sub.{name}(" in TEXT, f"未复用 sub.{name}"
    # 口径只此一处：本文件不得自行拼装 SQL 或直接调 db.execute/query。
    assert "execute(" not in TEXT and "executemany(" not in TEXT
    for kw in ("SELECT ", "INSERT INTO ", "UPDATE ", "DELETE FROM "):
        assert kw not in TEXT, f"出现了自行拼装的 SQL：{kw}"


def test_config_wiring_and_thread_safety():
    assert "sub_enabled" in TEXT and "sub_interval_min" in TEXT
    assert "save_config(self._cfg)" in TEXT
    assert "class _NetWorker(QThread)" in TEXT
    for token in ("requestInterruption", "isInterruptionRequested",
                  "wait(", "closeEvent", "def reject", "_set_busy(True)",
                  "_set_busy(False)", "finished.connect"):
        assert token in TEXT, f"缺少后台线程收尾要素：{token}"


# ---------------------------------------------------------------- 书面中文护栏
# 与 tests/test_v013_copy_professional_full.py 同口径，外加 r240 的补充词表。
COLLOQUIAL = ["点一下", "点开", "搞定", "顺手", "顺便", "其实", "反正",
              "大概", "差不多", "折腾", "费劲", "随便", "搞", "弄",
              "一下子", "试试", "没法", "不管了", "手动", "要找", "别找"]
MISSING = ["找不到", "还没", "还没有", "还没填", "还没设置", "还没下载"]
EXTRA = ["吧", "呢", "啊", "呀", "哈", "呗", "嘛", "喔", "你", "您", "咱",
         "俺", "咋", "啥", "一下", "千万", "绝对", "简直", "超级", "无敌",
         "翻车", "砸站", "手滑", "记住", "别忘", "点儿", "偷懒", "神器",
         "一键", "白费", "瞎", "乱杀", "骗人", "吓人"]
EXEMPT = ["整合", "整理", "整站", "完整", "统一", "唯一", "一次性",
          "同一", "哈希", "点开图", "一下（Ctrl"]
CJK = re.compile("[\u4e00-\u9fa5]")


def _user_strings(raw: str) -> list[str]:
    """与 v013 一致：剥 docstring 与整行注释，只取含中文的字符串字面量。"""
    t = re.sub(r'"""[\s\S]*?"""', '""', raw)
    t = re.sub(r"'''[\s\S]*?'''", "''", t)
    t = "\n".join(l for l in t.splitlines()
                  if not l.lstrip().startswith("#"))
    out = []
    for m in re.finditer(r"""["']([^"'\n]{2,300})["']""", t):
        s = m.group(1)
        if CJK.search(s):
            out.append(s)
    return out


def test_user_facing_copy_is_professional():
    bad = []
    for s in _user_strings(TEXT):
        t = s
        for e in EXEMPT:
            t = t.replace(e, "")
        for w in COLLOQUIAL + MISSING + EXTRA:
            if w in t:
                bad.append((s[:80], w))
    assert not bad, "界面文案出现口语或禁用词：\n" + "\n".join(
        f"  {s}  ← {w}" for s, w in bad)


def test_standard_wording_present():
    joined = "\n".join(_user_strings(TEXT))
    for w in ("单击", "未找到", "尚未", "暂无", "请"):
        assert w in joined, f"缺少规范用词：{w}"


def test_no_internal_ids_in_user_strings():
    """界面文案里不出现内部单号（#数字 后紧跟中文或中文标点）。"""
    pat = re.compile(r"#\d{1,3}\s*(?:[，。、）)】]|[\u4e00-\u9fa5])")
    for s in _user_strings(TEXT):
        assert not pat.search(s), f"界面文案含内部单号：{s[:80]}"


# ---------------------------------------------------------------- 纯函数
PURE = ("_zh_only", "_save_error_text", "_preview_text", "_check_text")


def _pure_ns():
    """把四个纯函数从 AST 抽出，单独 compile/exec，不导入整个模块。"""
    fns = [n for n in TREE.body
           if isinstance(n, ast.FunctionDef) and n.name in PURE]
    assert sorted(f.name for f in fns) == sorted(PURE)
    mod = ast.Module(
        body=[ast.ImportFrom(module="__future__",
                             names=[ast.alias(name="annotations")], level=0)]
        + fns,
        type_ignores=[])
    ast.fix_missing_locations(mod)
    ns: dict = {}
    exec(compile(mod, str(SRC), "exec"), ns)  # noqa: S102 - 仅本文件纯函数
    return ns


def test_pure_functions_are_chinese_and_pure():
    ns = _pure_ns()
    # 英文异常细节不进入用户可见说明
    assert ns["_zh_only"]("ConnectionError: boom") == ""
    assert ns["_zh_only"]("HTTPS 证书校验失败（自签名证书）") != ""
    err = ns["_save_error_text"]("链接规则不是合法的正则：missing )")
    assert "请" in err and "missing" not in err and ")" not in err
    # 预览：条数与明细
    text, body = ns["_preview_text"](
        {"ok": True, "count": 3, "links": ["a", "b", "c"], "truncated": False})
    assert "3" in text and body == "a\nb\nc"
    text, _ = ns["_preview_text"](
        {"ok": True, "count": 0, "links": [], "truncated": False})
    assert "未识别到任何条目" in text
    # 检查：两种口径说法分明
    dry, _ = ns["_check_text"](
        {"ok": True, "name": "示例", "found": 5, "new": 2,
         "items": ["u1"]}, False)
    assert "仅比对" in dry and "未创建任务" in dry
    run, _ = ns["_check_text"](
        {"ok": True, "name": "示例", "found": 5, "new": 2,
         "items": [], "task_id": 9}, True, True)
    assert "已创建采集任务" in run and "队列" in run
    # 失败：不把英文异常抛给用户
    fail, _ = ns["_check_text"](
        {"ok": False, "error": "ConnectionError: boom"}, False)
    assert "ConnectionError" not in fail and "boom" not in fail
