"""UI 文字中文化小工具（C5）。

Qt 自带的标准按钮（QDialogButtonBox.StandardButton.Close 等）**跟着系统语言走**，
英文系统上就显示成 "Close" / "OK" / "Cancel"。项目铁律是中文 UI，所以统一在
建完 button box 之后调一次 :func:`zh_buttons` 把它们改名。

为什么不直接 ``setText("确定")`` 散落在各处：
- 每个 dialog 都要写一遍，漏一处就又冒出个英文 "Close"（C5 就是这么来的）；
- 按钮的 ``StandardButton`` 角色还得留着（``rejected``/``accepted`` 信号靠它），
  只能改显示文字，不能换按钮类型。

用法::

    bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    bb.rejected.connect(dlg.reject)
    zh_buttons(bb)          # ← 加这一行，"Close" 就变 "关闭"
"""
from __future__ import annotations

# StandardButton 枚举值 → 中文。键用 int（枚举在 PySide6 里可直接当 int 用），
# 值是 None 表示"这个按钮本轮不管"（保持 Qt 默认，通常已是中文或不该出现）。
_ZH = {
    "Close": "关闭",
    "Ok": "确定",
    "Cancel": "取消",
    "Yes": "是",
    "No": "否",
    "Save": "保存",
    "Open": "打开",
    "Apply": "应用",
    "Reset": "重置",
    "Help": "帮助",
    "Discard": "放弃",
    "RestoreDefaults": "恢复默认",
    "YesToAll": "全部是",
    "NoToAll": "全部否",
}


def zh_buttons(box) -> list:
    """把 ``box`` 里的标准按钮显示文字改成中文，返回被改过的按钮列表。

    ``box`` 可以是 QDialogButtonBox 或 QMessageBox（两者都有 ``button()``）。
    **不翻译** QMessageBox 里带具体文案的按钮（如 "重试/忽略"）—— 那些是
    ``addButton(text, role)`` 建的，文字本来就是我们自己给的中文。

    幂等：重复调用只是把同样的文字再设一遍，不会叠加出 "关闭关闭"。
    """
    if box is None:
        return []
    out = []
    for std, zh in _ZH.items():
        try:
            btn = box.button(getattr(box.StandardButton, std))
        except Exception:
            # 老版本枚举名不同（如 Save 有别名）—— 拿不到就跳过，不猜。
            continue
        if btn is None:
            continue
        try:
            cur = btn.text() or ""
        except Exception:
            continue
        # 【关键】只翻译「还是 Qt 默认英文」的按钮。已经有自定义文案的
        # （比如 Ok 被 setText("导入") 改成业务词）一律不碰 —— 否则
        # zh_buttons 会把"导入"又改回"确定"，把功能标签弄丢。
        if cur and cur not in (std, std.lower(), std.upper(), zh):
            continue
        try:
            btn.setText(zh)
            out.append(btn)
        except Exception:
            continue
    return out


def audit_english_labels(root) -> list:
    """扫源码里的英文 UI 标签，返回 [(文件, 行号, 文本)]，供测试与自检用。

    只看**用户能看见的字符串**：QPushButton / QAction / setWindowTitle / addTab /
    QLabel 的第一个位置参数，以及 QDialogButtonBox 的标准按钮（那些由 Qt 按系统
    语言渲染，源码里看不到字面量，只能靠 :func:`zh_buttons` 兜）。

    误报会有一批（正则匹配不到跨行的、也没区分内部 id），所以定位到行给人看，
    不直接当失败条件 —— 真正的判定交给 tests/test_r60c_usability.py 里逐条钉死的断言。
    """
    import re
    from pathlib import Path

    pats = [
        re.compile(r'QPushButton\(\s*f?["\']([A-Za-z][^"\']{0,30})["\']'),
        re.compile(r'QAction\(\s*f?["\']([A-Za-z][^"\']{0,30})["\']'),
        re.compile(r'setWindowTitle\(\s*f?["\']([A-Za-z][^"\']{0,30})["\']'),
        re.compile(r'addLabel\(\s*f?["\']([A-Za-z][^"\']{0,30})["\']'),
    ]
    hits = []
    root = Path(root)
    for f in sorted(root.glob("*.py")):
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for i, line in enumerate(lines, 1):
            if line.lstrip().startswith("#"):
                continue
            for p in pats:
                m = p.search(line)
                if m:
                    hits.append((f.name, i, m.group(1)))
                    break
    return hits