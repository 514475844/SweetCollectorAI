"""V0.0.17 桌面端补齐模块的纯静态单测。

只读取源码做 ``ast`` / 字符串核验：不 import 任何 Qt 模块、不构造
QApplication、不打开对话框。覆盖四项契约签名、纯函数口径复用、无静态
QFileDialog、书面中文护栏、破坏性动作二次确认、扫码登录线程安全收尾。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "media_resource_manager"
GUI = PKG / "gui"

ONLINE = PKG / "online_models.py"
FULL = GUI / "fullbackup_dialog.py"
SYS = GUI / "sysinfo_dialog.py"
QR = GUI / "qrlogin_dialog.py"
SOURCES = {"online_models": ONLINE, "fullbackup_dialog": FULL,
           "sysinfo_dialog": SYS, "qrlogin_dialog": QR}

CJK = re.compile("[\u4e00-\u9fa5]")

# 与护栏同口径：书面中文禁用词（用户点名 + r240 词表 + 术语统一）。
FORBIDDEN = [
    "点一下", "点开", "搞", "搞定", "其实", "反正", "大概", "差不多",
    "折腾", "费劲", "随便", "还没", "还没有", "找不到", "还没填",
    "还没设置", "还没下载", "手动", "弄", "一下子", "试试", "没法",
    "不管了", "顺手", "顺便", "神器", "一键", "白费", "瞎",
    "你", "您", "咱", "俺", "吧", "呢", "啊", "呀", "哈", "呗", "嘛", "喔",
    "咋", "啥", "千万", "绝对", "简直", "超级", "无敌", "翻车", "砸站",
    "手滑", "记住", "别忘", "一下", "点儿", "偷懒", "乱杀", "骗人", "吓人",
    "要找", "别找",
]
EXEMPT = ["整合", "整理", "整站", "整张", "整体", "整个", "完整", "整数",
          "调整", "整齐", "整除", "一次性", "同一", "统一", "唯一", "哈希",
          "点开图", "一下（Ctrl"]
INTERNAL_ID = re.compile(
    r"(?<![A-Za-z0-9])R\d{2,3}(?!\d)|#\d{1,3}\s*(?:[，。、）)】]|[\u4e00-\u9fa5])")


# ---------------------------------------------------------------- 工具
def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _tree(p: Path) -> ast.Module:
    return ast.parse(_read(p))


def _func(tree, name):
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


def _cls(tree, name):
    for n in tree.body:
        if isinstance(n, ast.ClassDef) and n.name == name:
            return n
    return None


def _method(cls, name):
    for n in cls.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


def _user_strings(raw: str) -> list[str]:
    """与 test_v013_copy_professional_full 同口径：剥 docstring / 注释后取中文字符串。"""
    t = re.sub(r'"""[\s\S]*?"""', '""', raw)
    t = re.sub(r"'''[\s\S]*?'''", "''", t)
    t = re.sub(r"^\s*//.*$", "", t, flags=re.M)
    t = "\n".join(l for l in t.splitlines() if not l.lstrip().startswith("#"))
    out = []
    for m in re.finditer(r"""["'`]([^'"`\n]{2,300})["'`]""", t):
        s = m.group(1)
        if CJK.search(s):
            out.append(s)
    return out


def _hits(s: str) -> list[str]:
    t = s
    for e in EXEMPT:
        t = t.replace(e, "")
    return [w for w in FORBIDDEN if w in t]


# ---------------------------------------------------------------- 1. 签名
def test_all_sources_parse():
    for name, p in SOURCES.items():
        assert p.exists(), "%s 缺失" % name
        ast.parse(_read(p))


def test_contract_signatures():
    fn = _func(_tree(ONLINE), "fetch_models")
    assert fn is not None, "fetch_models 缺失"
    assert [a.arg for a in fn.args.args] == ["api_url", "api_key", "timeout"]
    assert [ast.unparse(a.annotation) for a in fn.args.args] == ["str", "str", "float"]
    assert ast.unparse(fn.returns) == "dict"
    assert [ast.literal_eval(d) for d in fn.args.defaults] == ["", 8.0]

    cases = [
        (FULL, "FullBackupDialog", ["self", "db", "cfg", "parent"]),
        (SYS, "SysInfoDialog", ["self", "cfg", "parent"]),
        (QR, "QRLoginDialog", ["self", "cfg", "parent"]),
    ]
    for path, cname, params in cases:
        cls = _cls(_tree(path), cname)
        assert cls is not None, "%s 缺失" % cname
        assert any("QDialog" in ast.unparse(b) for b in cls.bases), \
            "%s 未继承 QDialog" % cname
        init = _method(cls, "__init__")
        assert init is not None
        assert [a.arg for a in init.args.args] == params, \
            "%s.__init__ 签名不符" % cname
        assert len(init.args.defaults) == 1
        assert ast.literal_eval(init.args.defaults[0]) is None


# ---------------------------------------------------------------- 2. 口径复用
def test_fullbackup_and_online_models_reuse_pure_functions():
    full = _read(FULL)
    for name in ("prune_plan", "prune_and_delete", "inspect_zip",
                 "read_manifest", "is_full_name", "full_name",
                 "backup_dir", "human_size", "user_data_dir", "save_config"):
        assert name in full, "fullbackup_dialog 未复用 %s" % name

    online = _read(ONLINE)
    for token in ("/models", "Authorization", "Bearer ", "build_opener",
                  "json.loads", '"data"'):
        assert token in online, "online_models 缺少网页端口径 %s" % token

    sys = _read(SYS)
    assert "sysinfo.collect" in sys and "sysinfo.headline" in sys, \
        "sysinfo_dialog 未复用 sysinfo 纯函数"

    qr = _read(QR)
    for name in ("approve_qr_token", "reject_qr_token", "_qr_gc",
                 "_QR_TOKENS", "_QR_TOKEN_TTL", "token_urlsafe", "segno"):
        assert name in qr, "qrlogin_dialog 未复用 qr_login 口径 %s" % name


# ---------------------------------------------------------------- 3. 文件选择
def test_no_static_qfiledialog_and_uses_parent_pickers():
    bad_calls = {"getExistingDirectory", "getOpenFileName",
                 "getSaveFileName", "getOpenFileNames"}
    for name, p in SOURCES.items():
        raw = _read(p)
        for node in ast.walk(_tree(p)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in bad_calls, \
                    "%s 出现静态文件选择 %s" % (name, node.func.attr)
        for token in ("QFileDialog.getExistingDirectory(",
                      "QFileDialog.getOpenFileName(",
                      "QFileDialog.getSaveFileName(",
                      "QFileDialog.getOpenFileNames("):
            assert token not in raw, "%s 出现 %s" % (name, token)
    full = _read(FULL)
    assert "_file_pick" in full and "_dir_pick" in full, \
        "FullBackupDialog 未使用 parent._file_pick / _dir_pick"


# ---------------------------------------------------------------- 4. 书面中文
def test_no_colloquial_words_in_user_copy():
    # 自检：词表与扫描器本身有效，避免「空扫」导致断言永真。
    assert _hits("请点一下按钮"), "禁用词表失效"
    for name, p in SOURCES.items():
        assert _user_strings(_read(p)), "%s 未提取到用户可见字符串（扫描器失效）" % name
    bad = []
    for name, p in SOURCES.items():
        for s in _user_strings(_read(p)):
            h = _hits(s)
            if h:
                bad.append((name, s[:80], h))
    assert not bad, "新建模块出现口语化/非书面文案：\n" + "\n".join(
        "  %s: %s ← %s" % (n, s, h) for n, s, h in bad)


def test_no_internal_ids_in_user_copy():
    bad = []
    for name, p in SOURCES.items():
        for s in _user_strings(_read(p)):
            m = INTERNAL_ID.search(s)
            if m:
                bad.append((name, s[:80], m.group(0)))
    assert not bad, "界面文案出现内部单号：\n" + "\n".join(
        "  %s: %s ← %s" % (n, s, i) for n, s, i in bad)


def test_window_titles():
    assert '"完整备份管理"' in _read(FULL)
    assert '"本机配置"' in _read(SYS)
    assert '"手机扫码登录"' in _read(QR)


# ---------------------------------------------------------------- 5. 二次确认
def test_every_destructive_action_has_confirmation():
    raw = _read(FULL)
    cls = _cls(_tree(FULL), "FullBackupDialog")
    assert "QMessageBox" in raw, "破坏性动作未使用确认对话框"
    for m in ("_upload_and_restore", "_save_selected", "_do_prune"):
        seg = ast.get_source_segment(raw, _method(cls, m)) or ""
        assert "_confirm_action" in seg, "%s 缺少二次确认" % m
    prune_seg = ast.get_source_segment(raw, _method(cls, "_do_prune")) or ""
    assert "prune_plan" in prune_seg and "prune_and_delete" in prune_seg
    assert "将删除以下备份包" in prune_seg, "清理前未明确列出将删除的包名"
    assert "确认" in prune_seg


# ---------------------------------------------------------------- 6. 线程安全
def test_qrlogin_thread_safe_stop():
    raw = _read(QR)
    tree = _tree(QR)
    worker = _cls(tree, "_PollWorker")
    assert worker is not None
    assert any("QThread" in ast.unparse(b) for b in worker.bases), \
        "轮询未放在 QThread 子类"
    run_seg = ast.get_source_segment(raw, _method(worker, "run")) or ""
    assert "isInterruptionRequested" in run_seg, "轮询未做协作式中断检查"
    for ui in ("setText", "setPixmap", "setPlainText", "clicked"):
        assert ui not in run_seg, "worker 跨线程操作了界面控件 %s" % ui

    cls = _cls(tree, "QRLoginDialog")
    for m in ("reject", "closeEvent"):
        seg = ast.get_source_segment(raw, _method(cls, m)) or ""
        assert "_stop_worker" in seg, "%s 未安全停止线程" % m
    for token in ("requestInterruption", "wait(", "disconnect",
                  "_ORPHANS", "登录已确认"):
        assert token in raw, "qrlogin 收尾缺少 %s" % token
