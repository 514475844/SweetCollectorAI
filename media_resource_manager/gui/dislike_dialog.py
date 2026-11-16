"""“不喜欢”时弹出的学习对话框。

左栏（绿·白名单关键词 / 未命中的主题词）：该任务 AI 主题词里，这张图其实不该被主题收进来；
右栏（红·黑名单关键词，含已学进库 + 可手填）：普通黑名单 / AI 主题排除词，命中即应排除。

两栏并排、各栏独立滚动，且**互不重复**（白名单里若已出现在黑名单，则只留在黑名单，排除优先）。
每栏标题后带「全选」按钮（只选中本栏）；黑名单栏底部有编辑框，没命中时可手填不喜欢原因。
勾选/手填的词 → 加入任务 AI 排除词（下次运行自动跳过）；都不勾不填 = 单纯不喜欢。

#64 / A5-3：手填框接**词表桥接**——用户填「金发」，系统告诉他这会记到
「blond hair」上，并说清是词典对应还是 CLIP 算出来的相似度。
没有这一步，用户永远不知道自己学的是哪个词，闭环是黑盒。
"""
from __future__ import annotations

from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QCheckBox,
                              QLabel, QPushButton, QScrollArea, QWidget,
                              QFrame, QLineEdit)
from .theme import style as _tstyle      # P1-4：行内样式也要跟随皮肤


def _set_all(checks, val: bool):
    for c in checks:
        c.setChecked(val)


def _make_column(title: str, color: str, items: list[str],
                 footer=None, groups: list[tuple[str, list]] | None = None,
                 ) -> tuple[QWidget, list]:
    """构建一栏：标题 + 「全选」按钮 + 可滚动复选框（+ 可选 footer 编辑框）。

    返回 ``(container, column_checks)``；column_checks 仅本栏的复选框，
    保证「全选」只作用于本栏、不会误选另一栏。footer 固定在栏底部（不随滚动消失）。

    【R117 P1-3】``groups`` 非空时按 ``[(小标题, [items...]), ...]`` **分段渲染**
    —— 每段前面插一个不可勾选的小标题标签。用户要求「层级标注清晰」：
    左栏主题词来自 L1/L2/L3 三层，拍平成一列用户分不清哪个是几级，
    分段后一眼可辨。分段只影响**显示**，勾选出来的仍是干净词（不带前缀）。
    """
    container = QWidget()
    cv = QVBoxLayout(container)
    cv.setContentsMargins(0, 0, 0, 0)
    cv.setSpacing(4)
    # 标题行：标题 + 全选
    hdr = QHBoxLayout()
    th = QLabel(title)
    th.setStyleSheet(f"color:{color};font-weight:600;font-size:12px;")
    hdr.addWidget(th)
    hdr.addStretch(1)
    b_sel = QPushButton("全选")
    b_sel.setFixedSize(52, 22)
    _tstyle(b_sel,
            "QPushButton{background:{surface_alt};border:none;border-radius:6px;"
            "color:{text};font-size:12px;font-weight:600;}"
            "QPushButton:hover{background:{accent_soft_hover};}")
    hdr.addWidget(b_sel)
    cv.addLayout(hdr)
    # 滚动区
    box = QWidget()
    bv = QVBoxLayout(box)
    bv.setContentsMargins(2, 0, 2, 0)
    bv.setSpacing(4)
    column_checks: list[QCheckBox] = []

    def _add_items(items):
        for lab in items:
            c = QCheckBox(str(lab))
            c.setToolTip("勾选表示该图未匹配该主题词（一、二、三级均适用），"
                         "仅下调该词权重，不加入排除词")
            column_checks.append(c)
            bv.addWidget(c)

    if groups:
        # 分段渲染：空段不占位（没配二级就别显示「二级」标题，否则像是坏了）
        for gi, (gname, gitems) in enumerate(groups):
            if not gitems:
                continue
            if gi and column_checks:
                bv.addSpacing(6)
            gh = QLabel(str(gname))
            _tstyle(gh,
                    "color:{text_dim};font-size:11px;font-weight:600;")
            bv.addWidget(gh)
            _add_items(gitems)
        if not column_checks:
            empty = QLabel("（无）")
            _tstyle(empty, "color:{text_dim};font-size:12px;")
            bv.addWidget(empty)
    else:
        if not items:
            empty = QLabel("（无）")
            _tstyle(empty, "color:{text_dim};font-size:12px;")
            bv.addWidget(empty)
        _add_items(items)
    bv.addStretch(1)
    sa = QScrollArea()
    sa.setWidget(box)
    sa.setWidgetResizable(True)
    sa.setFrameShape(QFrame.Shape.NoFrame)
    cv.addWidget(sa, 1)
    if footer is not None:
        cv.addWidget(footer)
    b_sel.clicked.connect(lambda: _set_all(column_checks, True))
    return container, column_checks


def _bridge_preview(words, vocab: list[str]) -> str:
    """#64：手填词的桥接预演（纯函数，可离线测）。

    V0.0.12：单句文案实现抽到 label_bridge.preview_lines（网页判退模态
    显示同一句话，两处各写一份早晚分叉）。本函数保留原签名与「拼成
    一段多行文本」的桌面展示形态：
        金发 → blond hair（词典对应）
        猫 → cat（相似度 0.71）
        量子纠缠 → 不在词表里，只按原词记
    """
    from ..classify.label_bridge import LabelBridge, preview_lines

    if not words:
        return ""
    br = LabelBridge(vocab)
    return "\n".join(preview_lines(words, br))


def _sim_note(hit) -> str:
    if hit is None:
        return "没匹配上"
    if hit.source == "clip":
        return "相似度 —" if hit.sim is None else f"相似度 {hit.sim:.2f}"
    if hit.source == "dict":
        return "词典对应"
    return "就是词表里的词"


# ------------------------------------------------------------------ #
# #64：手填词的出口。
#
# 为什么用模块级暂存而不在返回值里加第4 项：ask_dislike_reason 的返回值被
# 4 处生产代码 + 2 个测试文件按**三元组**解包，改签名就是改一串调用方，
# 收益却只为了多带一个字段。而这个对话框是**模态**的 —— 同一时刻只可能有一份
# 「上一次手填了什么」，全局暂存不存在并发串号的可能。
# 读它之前记得 :func:`consume_manual_words`（取走并清空），别留着给下一次用。
_LAST_MANUAL: list[str] = []


def _remember_manual(words) -> None:
    global _LAST_MANUAL
    _LAST_MANUAL = [w for w in (words or []) if (w or "").strip()]


def last_manual_words() -> list[str]:
    """上一次弹窗里**用户手填**的词（不含勾选的排除词）。只读，不清空。"""
    return list(_LAST_MANUAL)


def consume_manual_words() -> list[str]:
    """取走并清空上一次的手填词。调用方在处理判定时立刻取，避免串到下一次。"""
    global _LAST_MANUAL
    out = list(_LAST_MANUAL)
    _LAST_MANUAL = []
    return out


# ------------------------------------------------------------------ #
# R174 §4：误标纠正 —— 「机器认错了，我想要的其实是 X」。
#
# 用户原话：「这几个单词其实是我想要的图片，只是机器认得不准，所以我删除了
# 或者点了不喜欢图片。这难道不是一个 bug 吗？」
#
# 单靠"不把模型标签写进黑名单"只是**止血**（不再屏蔽用户想要的东西），
# 但没有**给药**：系统仍然不知道用户真正想要什么。本节补的正是这一半 ——
# 用户勾「模型误标」并填「正确标签」后：
#   · 这张图仍记为负面（仅此图，与普通"不喜欢"一致）；
#   · 「正确标签」作为**正向**信号写入 preferences（user_typed, hit_pos+1），
#     帮系统学到"这类内容其实要"。
# 绝不写黑名单（这是本单的底线，与 unmatched 同口径）。
#
# 为什么同样用模块级暂存：`ask_dislike_reason` 的返回值被 4 处生产代码 +
# 多个测试按**三元组**解包，加第 4 项就是改一串调用方（同 _LAST_MANUAL 的理由）。
_LAST_CORRECT: list[str] = []


def _remember_correct(labels) -> None:
    global _LAST_CORRECT
    _LAST_CORRECT = [str(w).strip() for w in (labels or []) if str(w or "").strip()]


def last_correct_labels() -> list[str]:
    """上一次弹窗里用户声明的「正确标签」。只读，不清空。"""
    return list(_LAST_CORRECT)


def consume_correct_labels() -> list[str]:
    """取走并清空上一次的「正确标签」。与手填词同口径，取完即清。"""
    global _LAST_CORRECT
    out = list(_LAST_CORRECT)
    _LAST_CORRECT = []
    return out


def ask_dislike_reason(parent, labels: list[str], blacklist: list[str] | None = None,
                       hint: str = "", db=None, bridge_vocab=None,
                       labels_by_tier: dict | None = None,
                       ) -> tuple[list[str], list[str], str]:
    """返回 ``(unmatched, exclusions, mode)``。

    【语义（务必遵守，用户明确要求）】
    - ``unmatched``（左栏「主题词」勾选）：意思是**这张图其实没匹配上该主题词**，
      处理方式是给该词做负向偏好学习（把权重调低），
      **绝不能写进任务排除词**——否则整个主题都被误杀。
    - ``exclusions``（右栏「排除词」勾选 + 手填）：才是真正要加入 ``ai_blacklist``
      的词，下次运行自动跳过这类内容。
    - ``mode == "exclude"``：勾了任一词（或手填）。
      ``"pure"``：都没勾，单纯不喜欢。``"cancel"``：取消，什么都不做。

    #64：给了 ``db`` 就在手填框下面显示**词表桥接预演**（手填词会记到哪个
    词表标签上）。给不了 db（部分测试/无库场景）就不显示，不影响主流程。

    若 ``labels`` 和 ``blacklist`` 都为空，直接返回 ``([], [], "pure")``。
    """
    labels = list(labels or [])
    blacklist = list(blacklist or [])
    if not labels and not blacklist:
        return [], [], "pure"

    # 两栏互不重复：若同一词同时出现在白名单与黑名单，则只留在白名单，
    # 从黑名单移除（白名单优先——白名单的词绝不应出现在黑名单中）。
    label_set = set(labels)
    blacklist = [b for b in blacklist if b not in label_set]

    dlg = QDialog(parent)
    dlg.setWindowTitle("标记为不喜欢的理由")
    dlg.resize(680, 500)
    dlg.setMinimumSize(520, 380)
    v = QVBoxLayout(dlg)
    v.addWidget(QLabel(
        "确认后该图片将被删除：取决于「删除时先进回收站」设置 —— "
        "启用时移入隔离区可还原，关闭时永久删除。\n"
        "左栏【主题词 一/二/三级】：勾选表示该图片实际未匹配该主题词"
        "（二、三级为一级下的细分词），仅下调该词权重，不写入排除词。\n"
        "右栏【排除词】：勾选或手工填写表示排除此类内容，"
        "写入排除词后后续运行自动跳过。\n"
        "两栏均未勾选且未手工填写：仅标记为不喜欢（移除图片，不修改任何词）。"))
    if hint:
        h = QLabel(hint)
        h.setWordWrap(True)
        _tstyle(h, "color:{warn_text};font-size:11px;")
        v.addWidget(h)

    # 左 = 主题词（勾选=这张图其实"不匹配"该主题，只做负向学习），
    # 右 = 排除词（勾选=加入排除词，下次自动跳过）。两栏语义不同，绝不混为一谈。
    black_edit = QLineEdit()
    black_edit.setPlaceholderText("可手工填写排除原因（确认时一并加入排除词）")
    _tstyle(black_edit,
            "border:1px solid {border};border-radius:8px;padding:5px 8px;"
            "color:{text};background:{surface};")
    if db is not None:
        # #64：手填框一有内容就实时显示「会记到哪个词表标签上」。
        # 用 textChanged 而不是编辑确认后才算 —— 用户边打边看，不用猜。
        vocab = list(bridge_vocab) if bridge_vocab is not None else None
        if vocab is None:
            try:
                from ..classify.label_bridge import global_vocabulary
                vocab = global_vocabulary(db)
            except Exception:
                vocab = []
        _pv = QLabel("")
        _pv.setWordWrap(True)
        _tstyle(_pv, "color:{accent_deep};font-size:11px;")

        def _refresh_preview(_txt):
            _pv.setText(_bridge_preview([black_edit.text()], vocab))
        black_edit.textChanged.connect(_refresh_preview)
        _wrap = QWidget()
        _wv = QVBoxLayout(_wrap)
        _wv.setContentsMargins(0, 0, 0, 0)
        _wv.setSpacing(2)
        _wv.addWidget(black_edit)
        _wv.addWidget(_pv)
        black_footer: QWidget = _wrap
    else:
        black_footer = black_edit
    # P1-4：两栏标题色原先写死（绿 #16a34a / 红 #ef4444）。绿是「主题词」侧、
    # 红是「排除词」侧 —— 这是**语义色**（与 grade_* 同理，刻意不跟皮肤：
    # 跟皮肤走的话换肤后两栏可能同色，方向就分不出来了），只是从字面量
    # 收进调色板，深浅两套值各自合适。
    from .theme import c as _c, _CURRENT_DARK
    _dark = _CURRENT_DARK
    # 【R117 P1-3】左栏按 L1/L2/L3 分段渲染，层级一眼可辨。
    # 没给 labels_by_tier（老调用方/单测）时退回拍平列表，行为与原来一致。
    _groups = None
    if labels_by_tier:
        _groups = [("一级主题词", list(labels_by_tier.get(1) or [])),
                   ("二级主题词（一级的细分）", list(labels_by_tier.get(2) or [])),
                   ("三级主题词", list(labels_by_tier.get(3) or []))]
    sa_left, checks_left = _make_column(
        "主题词 一/二/三级（勾选表示该图未匹配该词）",
        _c(_dark, "ok", "#16a34a"), labels, groups=_groups)
    sa_right, checks_right = _make_column(
        "排除词（勾选后加入排除词）",
        _c(_dark, "danger", "#ef4444"), blacklist, footer=black_footer)
    cols = QHBoxLayout()
    cols.setSpacing(12)
    cols.addWidget(sa_left, 1)
    cols.addWidget(sa_right, 1)
    v.addLayout(cols, 1)

    # ---- R174 §4：误标纠正（可选的"我想要的其实是 X"） ----
    # 放在两栏**下面**、按钮行上面：它是"两栏都勾不对时"的补充出口，
    # 不是第三个并列的勾选框（不然用户会以为必须在三处选一个）。
    mis_box = QWidget()
    mv = QVBoxLayout(mis_box)
    mv.setContentsMargins(0, 4, 0, 0)
    mv.setSpacing(4)
    chk_mis = QCheckBox("模型误判：该图属于目标内容，被错误标记")
    chk_mis.setToolTip(
        "勾选表示该图片被模型错误标记。\n"
        "· 该图片仍会被删除或移入隔离区（与常规处理一致）；\n"
        "· 填写「正确标签」后，该词作为**正向**学习信号，"
        "后续将优先匹配此类内容；\n"
        "· 不会写入排除词（本功能用于纠正该类误判）。")
    mv.addWidget(chk_mis)
    correct_edit = QLineEdit()
    correct_edit.setPlaceholderText(
        "正确标签（可留空；多个标签以空格分隔，例如：红丝 长袜）")
    _tstyle(correct_edit,
            "border:1px solid {border};border-radius:8px;padding:5px 8px;"
            "color:{text};background:{surface};")
    # 没勾「模型误标」时输入框置灰 —— 一个看得见但要用不上的框，
    # 比"藏起来等勾了才出现"更可发现（用户先看到才会去勾）。
    correct_edit.setEnabled(False)
    chk_mis.toggled.connect(correct_edit.setEnabled)
    mv.addWidget(correct_edit)
    v.addWidget(mis_box)

    row = QHBoxLayout()
    b_exclude = QPushButton("加入排除并删除")
    b_exclude.setDefault(True)
    b_pure = QPushButton("单纯不喜欢")
    b_cancel = QPushButton("取消")
    row.addStretch(1)
    row.addWidget(b_exclude)
    row.addWidget(b_pure)
    row.addWidget(b_cancel)
    v.addLayout(row)

    # 两栏分开收集：
    #   unmatched  = 左栏（主题词）勾选 → 只做负向偏好学习，绝不能进排除词；
    #   exclusions = 右栏（排除词）勾选 + 手填 → 才写进任务 ai_blacklist。
    # #64：手填的词单独记在manual 里 —— 调用方要靠它走
    # ``learn_manual``（桥接到词表标签再学），勾选的排除词本来就是词表里的，
    # 不需要桥、也不该被当成「手填」。
    # R174 §4：correct = 「正确标签」——正向信号，同样绝不进排除词。
    result = {"mode": "cancel", "unmatched": [], "exclusions": [],
              "manual": [], "correct": []}

    def _collect_correct() -> list[str]:
        """取「正确标签」：没勾「模型误标」时一律忽略（勾了才算表态）。"""
        if not chk_mis.isChecked():
            return []
        return [w for w in correct_edit.text().split() if w.strip()]

    def do_exclude():
        unmatched = [c.text() for c in checks_left if c.isChecked()]
        excl = [c.text() for c in checks_right if c.isChecked()]
        custom = black_edit.text().strip()
        if custom:
            excl.append(custom)
        result["mode"] = "exclude" if (unmatched or excl) else "pure"
        result["unmatched"] = unmatched
        result["exclusions"] = excl
        result["manual"] = [custom] if custom else []
        result["correct"] = _collect_correct()
        dlg.accept()

    def do_pure():
        result["mode"] = "pure"
        result["unmatched"] = []
        result["exclusions"] = []
        result["manual"] = []
        # ⚠ 单纯不喜欢也允许带「正确标签」：用户点了不喜欢、但机器认错了，
        #   这正是用户原话描述的场景（"我删除了或者点了不喜欢"）。
        #   把它丢掉就等于让用户再走一遍流程，而系统还是学不到他要什么。
        result["correct"] = _collect_correct()
        dlg.accept()

    def do_cancel():
        result["mode"] = "cancel"
        result["unmatched"] = []
        result["exclusions"] = []
        result["manual"] = []
        dlg.reject()

    b_exclude.clicked.connect(do_exclude)
    b_pure.clicked.connect(do_pure)
    b_cancel.clicked.connect(do_cancel)

    dlg.exec()
    # ⚠️ 返回值**必须还是 3 元组**：现有调用方（含 tests/test_review_multiselect.py）
    # 全是 ``u, e, m = ask_dislike_reason(...)`` 三元解包，不能改签名。手填词改由
    # :func:`last_manual_words` 取（模块级暂存），不破坏任何既有调用方。
    # R174 §4：「正确标签」同理由 :func:`last_correct_labels` 取。
    _remember_manual(result["manual"])
    _remember_correct(result["correct"])
    return result["unmatched"], result["exclusions"], result["mode"]
