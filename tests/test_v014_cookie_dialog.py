"""V0.0.14 静态单测：``gui/cookie_dialog.py`` 的字面量 / 纯函数 / 契约检查。

全程**不 import PySide6、不构造 QApplication、不启动事件循环**：只读源码文本，
用 ``ast`` 解析，并把模块级纯函数抽出来在隔离命名空间里 ``exec`` 后验证行为。
因此本文件可单独运行，不会牵出 Qt，也不触碰任何现有模块的运行时状态。

词表里的口语词是「被检测的样本」，不是界面文案；护栏扫描的是产品码目录
（gui / webapp），不会把测试文件当界面。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "media_resource_manager" / "gui" / "cookie_dialog.py"
COOKIES = ROOT / "media_resource_manager" / "cookies.py"
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
               and ("cookies", "ck") in aliases
               for level, module, aliases in _imports()), \
        "必须使用 from .. import cookies as ck"
    assert any(level == 2 and module == "config"
               and ("save_config", None) in aliases
               for level, module, aliases in _imports()), \
        "必须从 ..config 导入 save_config"
    # 禁止单级相对导入：一个点会指向 gui 包内部并不存在的模块。
    for level, module, _ in _imports():
        assert not (level == 1 and module), "禁止 from .x import ..."


def test_dialog_signature_and_title():
    cls = next(n for n in TREE.body
               if isinstance(n, ast.ClassDef) and n.name == "CookieDialog")
    bases = [b.id for b in cls.bases if isinstance(b, ast.Name)]
    assert "QDialog" in bases
    init = next(n for n in cls.body
                if isinstance(n, ast.FunctionDef) and n.name == "__init__")
    assert [a.arg for a in init.args.args] == ["self", "cfg", "parent"]
    assert len(init.args.defaults) == 1  # parent=None
    assert 'setWindowTitle("站点 Cookie 管理")' in TEXT


def test_module_header_states_single_source_discipline():
    doc = ast.get_docstring(TREE) or ""
    assert "cookies.py" in doc and "口径" in doc, \
        "文件头须写明「口径只用 cookies.py 一处」的纪律"


def test_reuses_cookies_functions_only():
    for name in ("summary", "get_cookie", "normalize_domain", "set_cookie",
                 "del_cookie", "import_payload", "pair_code", "new_pair_code"):
        assert f"ck.{name}(" in TEXT, f"未复用 ck.{name}"
    assert "import_payload(payload, merge=True)" in TEXT
    assert "save=lambda: save_config(cfg)" in TEXT
    assert "save_config(cfg)" in TEXT
    assert "cfg.cookie_pair_code = code" in TEXT
    # 口径只此一处：本文件不得直接打开存储文件或调用私有读写。
    for kw in ("STORE_PATH", "ck._load", "ck._save", "cookies.json"):
        assert kw not in TEXT, f"绕过了 cookies.py 口径：{kw}"


def test_cookies_module_exposes_the_pure_api():
    """纯函数面：cookies.py 必须提供这些函数（ast 静态检查，不导入、不起 Qt）。"""
    ct = ast.parse(COOKIES.read_text(encoding="utf-8"), filename=str(COOKIES))
    defs = {n.name for n in ct.body if isinstance(n, ast.FunctionDef)}
    for name in ("all_cookies", "summary", "get_cookie", "set_cookie",
                 "del_cookie", "normalize_domain", "import_payload",
                 "rows_from_payload", "pair_code", "new_pair_code"):
        assert name in defs, f"cookies.py 缺少纯函数 {name}"


# ---------------------------------------------------------------- 书面中文护栏
# 与 tests/test_v013_copy_professional_full.py、test_v014_subs_dialog.py 同口径。
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
    """与 v013/v014 一致：剥 docstring 与整行注释，只取含中文的字符串字面量。"""
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
    for w in ("单击", "未找到", "尚未", "暂无", "手工填写", "请"):
        assert w in joined, f"缺少规范用词：{w}"


def test_no_internal_ids_in_user_strings():
    """界面文案里不出现内部单号（#数字 后紧跟中文或中文标点）。"""
    pat = re.compile(r"#\d{1,3}\s*(?:[，。、）)】]|[\u4e00-\u9fa5])")
    for s in _user_strings(TEXT):
        assert not pat.search(s), f"界面文案含内部单号：{s[:80]}"


def test_error_and_result_messages_are_complete_chinese():
    """错误提示是完整中文句子，不把英文异常细节抛给用户。"""
    for phrase in ("请填写有效的域名后再保存。",
                   "请填写 Cookie 头内容。",
                   "读取所选文件失败，请确认文件可访问且为 UTF-8 编码。",
                   "所选内容不是合法的 JSON，请提供 Cookie 导出文件的内容。",
                   "保存配置失败，请确认数据目录可写后重试。",
                   "未找到可导入的 Cookie，请检查内容是否包含域名与名称。"):
        assert phrase in TEXT, f"缺少完整中文提示：{phrase}"
    assert "str(e)" not in TEXT and "repr(e)" not in TEXT
    for s in _user_strings(TEXT):
        for token in ("Error", "Exception", "Traceback"):
            assert token not in s, f"错误提示夹带英文异常：{s[:80]} ← {token}"


# ---------------------------------------------------------------- 纯函数
PURE = ("_summary_text",)


def _pure_ns():
    """把纯函数从 AST 抽出，单独 compile/exec，不导入整个模块、不牵出 Qt。"""
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


def test_summary_text_is_pure_and_hides_cookie_values():
    f = _pure_ns()["_summary_text"]
    assert f({"count": 2, "names": ["cf_clearance", "csrftoken"],
              "more": 0}) == "2 条：cf_clearance、csrftoken"
    assert f({"count": 13, "names": ["a"] * 12, "more": 1}).endswith("…")
    assert f({}) == "0 条："
    # 摘要只含名称与条数，绝不夹带 Cookie 值
    text = f({"count": 1, "names": ["cf_clearance"], "chars": 4096,
              "more": 0})
    assert "4096" not in text and "=" not in text
