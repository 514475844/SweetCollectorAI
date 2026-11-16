"""Dark / light theme for the app (persisted via config.dark_mode).

Modern "dashboard" look: soft backgrounds, white/panel cards, rounded corners,
a single accent colour, subtle borders, generous spacing.
"""
from __future__ import annotations

import re

# Shared accent + radii (参考站风格：紫罗兰强调色)
ACCENT = "#8b7cf8"          # violet (light mode)
ACCENT_D = "#9d8cff"        # slightly brighter on dark
ACCENT_HOVER = "#7466e8"
TITLE_COLOR = "#7c6cf0"     # 标题文字紫（用户特别喜欢的配色）
RADIUS = "8px"

# ---- 皮肤（#10）：同一套深色/浅色 QSS，靠强调色区分 ----
SKINS = {
    "violet": ("#8b7cf8", "#9d8cff", "#7c6cf0", "#7466e8"),
    "teal":   ("#22c3a6", "#3fd8bd", "#16a085", "#129e85"),
    "blue":   ("#4a90e2", "#6aa6ee", "#3a7bd5", "#2f6fc0"),
    "amber":  ("#f7a325", "#ffb648", "#e8940f", "#d4870b"),
    "rose":   ("#ef5da8", "#f77cbb", "#d94a92", "#c43e82"),
    # steel：中性钢灰蓝。R59 起给「原始工程风」用 ——
    # 强调色互不相同那条铁律（test_accent_colors_differ_between_light_presets）
    # 规定浅色预设不能共用强调色，而 violet 归violet_look、teal 归
    # qbittorrent、blue 归现代风，只剩中性色可选。刻意压低饱和度，
    # 贴合「工程/原生」的调性，也不会在任何一张卡上跟语义色抢眼。
    "steel":  ("#5b7c99", "#6f93b3", "#4a6b85", "#3f5c73"),
}
SKIN_LABELS = {
    "violet": "紫罗兰", "teal": "青蓝", "blue": "海蓝",
    "amber": "暖橙", "rose": "玫瑰", "steel": "钢灰蓝",
}

# ══════════════════════════════════════════════════════════════════
# 外观预设（SKIN_ORDER：暗夜风提升为第 3 个正式皮肤）
#
# 改造前的模型是「5 个强调色 × 明暗开关」：用户要先点「深色/浅色模式」，
# 再去「皮肤（强调色）」里挑颜色 —— **两件事**，而且深色并不是一个皮肤，
# 只是个布尔开关。用户的原话是「深色模式咱们不是说要把它当成一个皮肤来做吗，
# 这样就能凑够 3 个皮肤」，所以这里把两件事合成一件：一个预设 = 一整套外观。
#
# ⚠ 为什么 `SKIN_LABELS`（5 个强调色）**不是**用户看到的那三个名字：
#   SKINS/SKIN_LABELS/SKIN_SOFT 是**取色引擎**（强调色四档 + 三个衍生物），
#   键必须严格与 SKINS 对齐，自绘控件与测试都按这套键取色；
#   PRESETS/PRESET_LABELS 才是**用户看到的选择器**（预设 = 强调色 + 明暗）。
#   两套键**故意不同**，所以别拿 SKIN_LABELS 去渲染选择器 —— 那是 R38 之后
#   最容易踩的「拿错表」坑。tests/test_r68_skin_presets.py 把这条钉死。
#
# 键名沿用英文短名（配置落盘用），界面上一律显示中文label。
# 第三个预设 `night` 走的是 PALETTE["dark"] 那套**独立写的**深色调色板，
# 不是把浅色 flip 一下 —— 深底浅字、告警色另配（见 PALETTE 注释）。
#⚠ R59（NATIVE_SKIN_ORDER §1）：皮肤 4 → 5，**顺序即菜单顺序**。
#   ·新增 `violet_look`（「紫罗兰风」）= **R58 之前 classic 的原样观感**，
#     一个像素都没变（accent 仍是 violet）。
#   · `classic`（「原始工程风」）改用中性钢灰蓝 `steel`，
#     并由后续段落（NATIVE_SKIN_ORDER §3）接管「真原生」渲染。
#   ⇒ 老用户升级后必须被迁移到 violet_look（见 config.migrated_preset_for），
#     否则他们会看到界面整个换样 —— 静默的观感回退。
PRESETS = {
    # key:     (label,        accent, dark)
    "classic":    ("原始工程风",   "steel",  False),
    "violet_look": ("紫罗兰风",     "violet", False),
    "modern":     ("现代风",       "blue",   False),
    "night":   ("暗夜风",     "violet", True),
    # #qB皮肤：仿 qBittorrent 的「顶栏工具条 + 分组侧栏 + 底部状态栏」框架。
    # 浅色（qB 默认浅色界面）。框架布局见本文件的 LAYOUTS —— 它与前三个
    # 预设**同一套代码**，只是按布局字典显示/隐藏/重排控件，不会另写一套 UI。
    #
    # ⚠ 强调色为什么是 teal 而不是 blue：blue 已经是「现代风」的强调色，
    #   而 tests/test_r68_skin_presets.py 钉死了「浅色预设不得共用强调色」
    #   （两个浅色皮肤长得一样等于没做）。qB 的辨识度靠的是**框架**
    #   （顶栏工具条 + 分组侧栏 + 状态栏），强调色再取一个没被占用的 teal，
    #   既守住那条铁律，也让四个皮肤两两可分。
    "qbittorrent": ("qBittorrent风", "teal",   False),
}
# V0.0.19-E：默认皮肤改「现代风」（用户拍板整体现代化）。
# ⚠ 换默认皮肤那天，下面 DEFAULT_ACCENT 必须跟着改成新默认的强调色，
#   test_r68 的「钉关系不钉值」护栏会当场抓到不一致。
DEFAULT_PRESET = "modern"
# 兜底用的强调色**键名**。要「默认强调色的色值」时用这个再查 SKINS，
# 而不是各处写死 hex —— 写死会被 P1-4 的静态护栏拦下，而且换个默认皮肤
# 就会漏改（这正是 webui 第一版踩的坑）。
#⚠ 必须等于 `preset_accent(DEFAULT_PRESET)`。R59 起 classic 的强调色是
#   steel，写死 violet 会让 webui 的兜底强调色与桌面默认皮肤对不上。
#   tests/test_r68_skin_presets.py 有钉这条关系的护栏（钉关系不钉字面量）。
DEFAULT_ACCENT = "blue"


# ══════════════════════════════════════════════════════════════════
# 自定义皮肤（R160 #85）：把 .qss 丢进「数据目录/skins/」就能在皮肤菜单里选
#
# 为什么是「叠加层」而不是「再加一个内建预设」：
#   内建预设 = 强调色 + 明暗 + 框架布局，全在代码里编好；自定义皮肤只是
#   **在某个内建预设之上追加一段 QSS**。用户改几行颜色就见效，不必抄写
#   几百行基础样式，也不会因为少写字段而在下个版本变成半成品外观。
#
# 键空间是 `custom:<文件名>`。带前缀是为了**永不与内建键撞车** —— 万一用户
#   放了个 classic.qss，也不能把老配置里那个 classic 顶掉。
#
# 叠加在哪个预设上：文件里写一行 `/* mrm-base: night */`；没写就回落默认
#   预设。刻意**不给 config 加 ui_preset_base 字段** —— 多一个字段就多一处
#   「两边不一致」的可能，而皮肤文件自己带得动这条信息。
# ══════════════════════════════════════════════════════════════════
CUSTOM_PREFIX = "custom:"
CUSTOM_DIRNAME = "skins"
CUSTOM_QSS_SUFFIX = ".qss"
CUSTOM_CSS_SUFFIX = ".css"
CUSTOM_SUFFIXES = (CUSTOM_QSS_SUFFIX, CUSTOM_CSS_SUFFIX)
# 单个皮肤文件的大小上限。放这个闸是因为 QSS 是被 Qt **同步解析**的，
# 一个几十 MB 的皮肤文件会让启动卡在 setStyleSheet 里，看着像程序崩了。
CUSTOM_MAX_BYTES = 256 * 1024
CUSTOM_MAX_NAME = 64
_CUSTOM_BASE_RE = re.compile(r"mrm-base\s*[:=]\s*([A-Za-z0-9_-]{1,32})")
_CUSTOM_KEY_LEN = len(CUSTOM_PREFIX)
# 名字里不许有：路径分隔符（穿越）、Windows 保留字符、控制字符。
# ⚠ 刻意**允许中文** —— 给皮肤起名「我的暗色」是正常需求，卡掉更难受。
_CUSTOM_BAD_NAME_RE = re.compile(r"[/\\\x00-\x1f:*?\"<>|]")


def custom_skins_dir():
    """自定义皮肤目录（数据目录/skins）。目录不存在也照样返回路径。"""
    from ..config import user_data_dir
    return user_data_dir() / CUSTOM_DIRNAME


def custom_key_of(stem: str) -> str:
    return CUSTOM_PREFIX + stem


def custom_stem_of(key):
    """`custom:xxx` → `xxx`；不是自定义键返回 None（绝不抛）。"""
    if not isinstance(key, str) or not key.startswith(CUSTOM_PREFIX):
        return None
    return key[_CUSTOM_KEY_LEN:].strip() or None


def is_custom_skin(key) -> bool:
    return custom_stem_of(key) is not None


def custom_name_ok(name) -> bool:
    """文件名（不含扩展名）能不能当皮肤名。"""
    if not isinstance(name, str):
        return False
    n = name.strip()
    if not n or len(n) > CUSTOM_MAX_NAME or n in (".", ".."):
        return False
    return not _CUSTOM_BAD_NAME_RE.search(n)


def _custom_path(stem, suffix):
    """stem + 后缀 → 目录内的真实路径；名字不合法返回 None。

    ⚠ 名字里已禁掉两个路径分隔符，所以拼出来的文件**必然**落在皮肤目录内 ——
      这就是防路径穿越的全部依据，别再加一层 resolve() 比对（那层对
      符号链接其实是假安全，会给人一种「验过了」的错觉）。
    """
    if not custom_name_ok(stem):
        return None
    return custom_skins_dir() / (stem + suffix)


def _read_capped(path) -> str:
    """读文件，超长截断、编码错误跳过、任何异常都退回空串。"""
    try:
        if path is None or not path.is_file():
            return ""
        with path.open("rb") as fh:
            return fh.read(CUSTOM_MAX_BYTES).decode("utf-8", errors="ignore")
    except Exception:
        return ""


def list_custom_skins() -> list:
    """扫描皮肤目录 → [(key, label), ...]（按名排序、同名只算一个）。

    ⚠ 目录不存在 / 不可读 / 里面混着别的文件 ⇒ 返回空列表，**绝不抛**。
      这个函数在建菜单和 Web 设置页的路径上，一次 IO 抖动不该让窗口起不来。
    ⚠ .qss 与同名的 .css 算**同一个皮肤**（后者是网页端的补充样式，
      不是第二个皮肤），否则菜单里会出现两个一模一样的名字。
    """
    found = {}
    try:
        for p in custom_skins_dir().iterdir():
            try:
                if not p.is_file():
                    continue
                suf = p.suffix.lower()
                if suf not in CUSTOM_SUFFIXES:
                    continue
                stem = p.name[: -len(suf)]
                if custom_name_ok(stem):
                    found[stem] = True
            except Exception:
                continue
    except Exception:
        return []
    return [(custom_key_of(s), s) for s in sorted(found)]


def custom_skin_qss(key) -> str:
    """自定义皮肤的 QSS 正文（叠加层）。没这份文件就返回空串。"""
    stem = custom_stem_of(key)
    if not stem:
        return ""
    return _read_capped(_custom_path(stem, CUSTOM_QSS_SUFFIX))


def _neutralize_style_close(css: str) -> str:
    """抹掉 `</style` 这个记号（大小写/空白无关）。

    ⚠ 这是**安全闸**不是排版：网页端把皮肤 CSS 注入一个 `<style>` 块，
      皮肤文件里只要带一个 `</style`，就能提前闭合它、把后面的内容当 HTML
      解析 —— 等于给皮肤文件发了张「任意 HTML」的通行证。破了记号的形就够，
      CSS 正文基本不受影响。
    """
    return re.sub(r"<\s*/\s*style", "/* mrm */ /style", css, flags=re.I)


def custom_skin_web_css(key) -> str:
    """网页端对应的 CSS（同名 .css，可选）。没这份文件就返回空串。"""
    stem = custom_stem_of(key)
    if not stem:
        return ""
    return _neutralize_style_close(_read_capped(_custom_path(stem, CUSTOM_CSS_SUFFIX)))


def custom_skin_base(key) -> str:
    """自定义皮肤叠加在哪个内建预设上。没写 / 写错 → 默认预设。"""
    m = _CUSTOM_BASE_RE.search(custom_skin_qss(key))
    if m and m.group(1) in PRESETS:
        return m.group(1)
    return DEFAULT_PRESET


def custom_skin_label(key) -> str:
    """自定义皮肤的用户可见名 = 文件名（去扩展名）。"""
    return custom_stem_of(key) or preset_label(DEFAULT_PRESET)


def resolve_preset_key(key) -> str:
    """把任意来源的皮肤值归一成合法键：内建原样 / 自定义原样 / 都不是→默认。

    以前各处都是 `key if key in PRESETS else DEFAULT_PRESET` 这么写一遍，
    加了自定义皮肤之后这份判断要改四处；收成一处才不会漏。
    """
    if is_custom_skin(key):
        return key
    return key if key in PRESETS else DEFAULT_PRESET


def preset_info(key):
    """预设 → (label, accent, dark)。未知键回落到默认预设，不抛 KeyError。

    配置文件里的值是用户/旧版本写进来的，**不能假定它一定合法**
    （手改config.json、跨版本升级都可能带来未知键）。取色路径高频调用，
    在这里炸掉等于整窗打不开。

    ⚠ R160：自定义键取**它叠加的那个内建预设**的三要素（强调色/明暗/label
      骨架），只有 QSS 走用户文件 —— 见本文件「自定义皮肤」段。
    """
    if is_custom_skin(key):
        return PRESETS.get(custom_skin_base(key), PRESETS[DEFAULT_PRESET])
    return PRESETS.get(key or DEFAULT_PRESET, PRESETS[DEFAULT_PRESET])


def preset_label(key) -> str:
    """用户可见的皮肤名（界面与日志统一走这里，避免各处硬写中文）。"""
    if is_custom_skin(key):
        # 自定义皮肤的名字是**文件名**，不能回落成内建预设名 ——
        # 否则菜单里会出现两个「原始工程风」，用户分不清点了哪个。
        return custom_skin_label(key)
    return preset_info(key)[0]


def preset_accent(key) -> str:
    """预设该用哪个强调色（SKINS 的键）。"""
    return preset_info(key)[1]


def preset_dark(key) -> bool:
    """预设是不是暗底。**深浅由预设决定，不再是独立开关**。"""
    return preset_info(key)[2]


# ---- #R60 真原生：哪些预设走「Windows 原始观感」------------------------
#
# 用户原话：「原始工程风的皮肤不太对吧？不是应该是 Windows 的原始窗口吗。
# 为什么好多按键还是自绘的？」（NATIVE_SKIN_ORDER §0）
#
# 「真原生」= **不套自绘 QSS、不设 QPalette、行内样式一并失效、图标走系统**，
# 全部交给 Windows 自己画。v1 只有 classic 是原生（其余四个观感不动，
# 避免回归）。新增原生皮肤时**只往这个集合里加键**，不要改 apply_theme。
NATIVE_PRESETS = frozenset({"classic"})


def preset_is_native(key) -> bool:
    """该预设是否走「真原生」渲染。**未知键按默认预设判**，不抛 KeyError。

    ⚠ 这里只回答「该不该原生」，**不负责执行**；执行在 apply_theme 的
      native 分支。拆开是因为 set_skin/主窗口建壳也要先知道答案。

    ⚠ 必须走「未知键→默认预设」这条回落，不能直接 `key in NATIVE_PRESETS`：
      配置文件里的值是用户/旧版本写进来的，手改 config.json 会出未知键。
      `preset_info`/`preset_label`/`preset_accent`/`preset_dark` 全都把
      未知键**回落成默认预设**，本函数若不跟着回落，就会出现
      「标签/取色都按 classic 走、原生判定却不原生」的**半吊子外观** ——
      而未知键在 apply_preset 里本来就先被归一成 classic，两处不一致
      等于同一件事有两个真相（R59 铁律：「恰好等于」不是契约）。
    """
    resolved = custom_skin_base(key) if is_custom_skin(key) else (
        key if key in PRESETS else DEFAULT_PRESET)
    return resolved in NATIVE_PRESETS


# ══════════════════════════════════════════════════════════════════
# 皮肤对应的「框架布局」（#qB皮肤：同一套代码，按预设换控件位置/显隐）。
#
# 为什么单独一张表而不是把布局塞进 PRESETS 的元组：
#   PRESETS 元组是 (label, accent, dark) 三要素，已经被取色路径与测试
#   钉死（tests/test_r68_skin_presets.py 校验「经典/现代/暗夜」三件套 +
#   SKIN_LABELS ≠ PRESET_LABELS）。布局是**框架层**的事，跟取色无关，
#   塞进去会把那张表的语义搅浑，也容易被测试误伤。这里单独维护，键与
#   PRESETS 对齐即可。
#
# 每个布局字典的字段：
#   toolbar      顶栏工具条（新建/全部开始/全部停止/编辑/设置/代理…）是否显示
#   statusbar    底部状态栏（QStatusBar，队列状态 + 统计）是否显示
#   sidebar      "left" / "right" / "none" —— 侧栏位置（v1 只实做 left）
#   grouped      侧栏是否用 qB 式「分组树」（分组标题 + 数量角标），
#                False=原有的平铺图标导航（任务/审核/图库）
#   bottom_panel 任务页底部「日志统计」面板是否显示
#   task_view    任务列表用哪套实现（NATIVE_SKIN_ORDER §2）：
#                  "table" = gui/task_table.py 的qB 式原生高密度表格
#                            （QTreeView + QStyledItemDelegate）
#                  "cards" = 现有的 TaskRow 卡片（QListWidget + setItemWidget）
#                ⚠ **两套并存，绝不删 TaskRow** —— classic/qbittorrent 用
#                table，其余三个皮肤保持 cards 不动（改动面最小、不引入回归）。
#   ⚠⚠ 新增字段时**必须同时更新 tests/test_r68_skin_presets.py 的键集校验**
#      和 test_r61 的一致性护栏；只加在 LAYOUTS 里而没人校验的话，
#      半年后没人知道哪些预设覆盖了这个字段。
#
# ⚠ 设计原则：主窗口**始终把两套控件都建出来**，只是按这张字典显隐/调宽。
#   这样切皮肤时根本不用重建整个窗口（重建会丢三页状态、风险高），
#   set_skin 末尾调一次 _apply_layout_visibility() 即可完成「换框架」。
LAYOUTS = {
    "classic":     {"toolbar": False, "statusbar": False, "sidebar": "left", "grouped": False, "bottom_panel": True, "task_view": "table"},
    # violet_look = R58 之前 classic 的原观感，框架层也一模一样。
    # 键必须与 PRESETS 对齐（preset_layout 未知键会回落到默认布局）。
    "violet_look": {"toolbar": False, "statusbar": False, "sidebar": "left", "grouped": False, "bottom_panel": True, "task_view": "cards"},
    "modern":      {"toolbar": False, "statusbar": False, "sidebar": "left", "grouped": False, "bottom_panel": True, "task_view": "cards"},
    "night":       {"toolbar": False, "statusbar": False, "sidebar": "left", "grouped": False, "bottom_panel": True, "task_view": "cards"},
    # qBittorrent：顶栏工具条 + 分组侧栏 + 底部状态栏，全开。
    "qbittorrent": {"toolbar": True,  "statusbar": True,  "sidebar": "left", "grouped": True,  "bottom_panel": True, "task_view": "table"},
}
# V0.0.19-E：默认布局跟着 DEFAULT_PRESET 走（以前钉死 classic 的表格布局，
# 默认皮肤改 modern 后这里不跟，未知键回落到"半 classic 半 modern"的混搭）。
DEFAULT_LAYOUT = LAYOUTS[DEFAULT_PRESET]


def preset_layout(key) -> dict:
    """预设 → 框架布局字典。未知键回落默认布局，不抛 KeyError。

    主窗口建壳与 set_skin 切肤都走这里，保证「布局」这一件事只有一处真相。

    ⚠ R160：自定义皮肤没有自己的布局，用**它叠加的那个内建预设**的布局
      （布局是框架层的框架，用户改颜色不该顺带把顶栏/状态栏改没了）。
    """
    if is_custom_skin(key):
        return LAYOUTS.get(custom_skin_base(key), DEFAULT_LAYOUT)
    return LAYOUTS.get(key or DEFAULT_PRESET, DEFAULT_LAYOUT)


# 当前预设（apply_preset 时更新）。与 _CURRENT_SKIN/_CURRENT_DARK 分开：
# 那两个是「取色时该用哪一档」的结果，本项是「用户选了哪个」。
_CURRENT_PRESET = DEFAULT_PRESET

# 当前是否「真原生」（apply_theme 的 native 分支写入）。
#
# ⚠⚠ 这里**刻意不从 _CURRENT_PRESET 推导**（R60 头「坑1」）：
#   apply_theme(app, dark, skin) 被测试直接调了 43 处，且模块刚 import 时
#   _CURRENT_PRESET 就是 "classic"（= 原生预设）⇒ 隐式推导会让那 43 处
#   全部一夜变原生，把 R57b 护栏（调色板/非空 QSS 断言）一起打爆，
#   而且看起来完全像真回归。改为**显式参数 + 单一写入点**。
_NATIVE_MODE = False


def native_mode() -> bool:
    """当前是否处于真原生渲染（行内样式是否应整体失效）。"""
    return _NATIVE_MODE


def current_preset() -> str:
    return _CURRENT_PRESET


def _skin_colors(skin):
    return SKINS.get(skin or "violet", SKINS["violet"])


# ---- P1-4 第二步：强调色的「衍生物」色位（柔和底 / 悬停底 / 深文字）----------
# 为什么单独一张表而不是用 mix() 算：
#   · soft / hover 是**背景**，混色能算准；
#   · deep 是**文字**。把强调色往黑/白混会顺带把饱和度也拉掉
#     （#8b7cf8 混黑 55% 得到 #4f4b8a，是灰紫，不是 #5b3fd6 那种饱和深紫），
#     而文字色只能靠眼睛定 —— 它还是皮肤识别度的关键，算错一眼就看得出来。
# 所以三个色位逐皮肤手挑。**violet 一列刻意与收敛前的行内字面量逐位相同**
# （#efeaff / #e0d6ff / #5b3fd6），保证这次收敛在默认皮肤下**零视觉变化**，
# 改动只对「换皮肤后不跟随」这个真问题生效。
SKIN_SOFT = {
    "violet": ("#efeaff", "#e0d6ff", "#5b3fd6"),
    "teal":   ("#e0f7f1", "#c8f0e6", "#0b7a63"),
    "blue":   ("#e8f1fc", "#d2e5f8", "#2a5f9e"),
    "amber":  ("#fdf1de", "#fae2ba", "#96610a"),
    "rose":   ("#fdeaf4", "#fbd3e6", "#a62a6b"),
    "steel":  ("#e8eef3", "#d5e0ea", "#2f5468"),
}

# ---- 深色下的「带强调色倾向」色位（SKIN_ORDER 补）----
# ⚠ 为什么必须单独一张表，而不能拿 SKIN_SOFT 取反：
#   SKIN_SOFT 那三档是**浅底**（选中项的底、悬停底）。深色下对应的
#   是「深底 + 一点点强调色倾向」，取反会得到一个**惨白**的选中块
#   —— 那就是 R38 抓到的「白卡片」同款问题（暗色里一块块浅色 = 瞎）。
#   所以这三档逐皮肤手挑，且**刻意压得很暗**（明度与 surface 同档）。
SKIN_TINT_DARK = {
    #key:     (选中底,   选中描边, 分组标题字)
    "violet": ("#2a2745", "#4a4270", "#b0a6ff"),
    "teal":   ("#1d3b38", "#2f5c56", "#7fd8c6"),
    "blue":   ("#1e2f45", "#33517a", "#9ec4f0"),
    "amber":  ("#3d2f1c", "#5e4a2c", "#f0c07a"),
    "rose":   ("#3b2334", "#5e3a53", "#f2a3cb"),
    "steel":  ("#22303a", "#37505f", "#9fc0d6"),
}


# ══════════════════════════════════════════════════════════════════
# 完整调色板（P1-4：皮肤以前只能改 QSS 覆盖到的颜色，自绘控件不跟随，
# 用户觉得「皮肤是半成品」。这里把语义化的色位集中成一张表，自绘处一律
# 从这里取色 —— 不再在 gui/ 各处写字面量。
#
# ⚠ 为什么不用 f-string 直接把 QSS 全模板化：那会把 DARK_QSS/LIGHT_QSS
#   变成几百行插值，任何一个占位错位都静默变成非法 QSS 被 Qt 丢弃
#   （实测踩过：加了规则却一点没生效还查不出原因）。这里走**追加替换**：
#   调色板只在自绘/调色板路径生效，QSS 那套成熟替换（_recolor）不动。
# ══════════════════════════════════════════════════════════════════
PALETTE: dict = {
    "dark": {
        "bg":          "#0f1115",   # 窗口底
        "surface":     "#171a21",   # 卡片 / 输入框底
        "surface_alt": "#1c2029",   # 次级面（按钮、hover）
        "border":      "#2a2f3a",
        "text":        "#e7eaf0",
        "text_dim":    "#aab1bd",
        "on_accent":   "#ffffff",   # 强调色之上的文字
        # 语义告警色（横幅 / 提示行 / 危险操作按钮）。
        # 深色下不是把浅色调暗，而是**整体换成暗底浅字** —— 告警要的是
        # 「跳出来」，暗底配深字在深色主题里等于隐形。
        "warn_bg":     "#3a2a16",
        "warn_border": "#6b4a24",
        "warn_text":   "#f0b27a",
        "warn_solid":  "#8a5a2a",   # 告警按钮实底（其上文字用 on_accent）
        "danger":      "#ef4444",   # 删除 / 清空这类危险操作
        "neg":         "#e2703a",   # 「排除 / 不喜欢」方向（固定橙红，不跟皮肤）
        "ok":          "#22c55e",   # 「已就绪」这类成功态（HTML 富文本里用）
        # ---- P2 收口补的语义位（原先这些色是各文件写死的字面量）----
        # 「三级文字」：比 text_dim 再弱一档，用在统计数字/单位这类装饰性文字上。
        # 以前 main_window 的 c_st 就是写死的浅灰，深色下和底色对比不足。
        "text_faint":  "#8a93a3",
        # 任务状态点（迅雷式任务行左侧那颗圆点）。语义上它表达的是
        # 「跑到哪一步了」，**刻意不跟随皮肤** —— 跟皮肤走的话，
        # 「失败」在某些皮肤下会和强调色撞色，状态就读不出来了。
        "st_running":  "#22c55e",
        "st_queued":   "#f59e0b",
        "st_idle":     "#6b7280",
        # ⚠ R128：`st_done` 原先写死 #8b7cf8，与 violet 皮肤的强调色
        #   **逐位相同** —— 于是「已完成」那颗状态点在**任何**皮肤下都是
        #   紫罗兰色：换到现代风（蓝）还是紫的，切到真原生（classic）
        #   画面里仍留着自绘紫点（违反「原生模式完全交给系统样式」）。
        #   这里改回**语义色**（青绿），并把 palette() 里那句「跟随皮肤」
        #   的覆盖一并去掉：状态点按设计就不该跟皮肤（见上方注释）。
        "st_done":     "#14b8a6",
        "st_error":    "#ef4444",
        "st_stopped":  "#f59e0b",
        # 「达标 / 偏低 / 不合格」三档评分色（HTML 富文本里用）。
        # 与状态点同理：这是**好坏**语义，跟皮肤走会失去含义。
        "grade_ok":    "#22c55e",
        "grade_warn":  "#f59e0b",
        "grade_bad":   "#ef4444",
        # 卡片底 + 卡片描边：概览数字卡片那种「浮在面板上的一块」用。
        # ⚠ 2026-10-08 修（R38 换肤像素测试实测抓出）：这两个键原先在
        #   **dark 下也留着近白淡紫**（#f6f5ff / #f6f3ff），是P1-4 收敛时
        #   只顾着把字面量搬进调色板、没跟着深浅各给一套真值。
        #   后果：暗色模式里学习看板的概览卡片是**一块块白卡片**，
        #   卡片上的深色数字就成了「白底黑字」——正是作业单点名的
        #   「暗色下文字/背景对比可读（无白底黑字逆色）」那一项。
        #   静态护栏（TOTAL_REAL=0）抓不到它：值确实来自调色板，
        #   只是**调色板自己填错了** —— 这类错只有真截图才看得见。
        "card_bg":     "#1f2430",   # 比 surface 略亮，卡片才「浮」得出来
        "card_border": "#2a2f3a",
        # 列表项 hover 底（相似图推荐那种卡片）：
        "row_hover":   "#232838",
    },
    "light": {
        "bg":          "#eef1f6",
        "surface":     "#ffffff",
        "surface_alt": "#f4f6fa",
        "border":      "#d6dbe4",
        "text":        "#1f2430",
        "text_dim":    "#5a6271",
        "on_accent":   "#ffffff",
        "warn_bg":     "#fff4e5",
        "warn_border": "#f0b27a",
        "warn_text":   "#8a4b00",
        "warn_solid":  "#f0b27a",
        "danger":      "#ef4444",
        "neg":         "#c2410c",
        "ok":          "#16a34a",
        # ---- P2 收口补的语义位（与 dark 同键，值按深浅各自取值）----
        # ⚠ R80 项11：浅色下原值 #6a7080 对 bg #eef1f6 只有 **4.37**
        #   （WCAG 正文线 4.5 都没过）—— 任务行「状态」列（TaskRow st，
        #   全仓唯一 product 用法）12px 小字挂这个色，正是「列表文字
        #   看不清」的一角。压深到 #5f6573 = 5.11，三档层次仍保留
        #   （text 13.7 > text_dim 5.42 > text_faint 5.11，全部 ≥4.5）。
        "text_faint":  "#5f6573",
        "st_running":  "#22c55e",
        "st_queued":   "#f59e0b",
        "st_idle":     "#6b7280",
        # R128：同上，语义青绿（浅底上用更深一档，对比度 5.47 ≥ 4.5）。
        "st_done":     "#0f766e",
        "st_error":    "#ef4444",
        "st_stopped":  "#f59e0b",
        "grade_ok":    "#22c55e",
        "grade_warn":  "#f59e0b",
        "grade_bad":   "#ef4444",
        # 卡片底在浅色下不能是淡紫（会和面板底几乎同色，看着像没画框），
        # 所以浅色用纯白 + 淡描边，靠描边把卡片边界交代清楚。
        "card_bg":     "#fbfaff",
        "card_border": "#ddd8f5",
        "row_hover":   "#f6f3ff",
    },
}


# ══════════════════════════════════════════════════════════════════
# WCAG 对比度工具（R107：表格/列表正文可读性护栏）
#
# 所有皮肤 × 所有表格/列表的「前景/背景」对都必须 ≥ 4.5:1（WCAG AA 正文线）。
# 这里集中实现相对亮度 + 对比度公式，供护栏测试与取色逻辑共用 —— **不编造数字，
# 真算**。tests/test_r107.py 单测这三个函数，并逐皮肤对关键 fg/bg 对断言 ≥4.5。
# ══════════════════════════════════════════════════════════════════
def _hex_to_rgb(h: str):
    """`#rgb` / `#rrggbb` → (r, g, b) 整数元组。非 hex 兜底黑。"""
    h = (h or "#000000").lstrip("#")
    if len(h) == 3:
        h = h[0] * 2 + h[1] * 2 + h[2] * 2
    h = (h + "000000")[:6]
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _rel_lum(rgb) -> float:
    """WCAG 2.1 相对亮度（0=黑，1=白）。rgb 为 (r,g,b) 0~255。"""
    def _lin(v):
        v = v / 255.0
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def _wcag_contrast(c1, c2) -> float:
    """WCAG 2.1 对比度（≥4.5 = AA 正文线）。c1/c2 为 (r,g,b) 或 #hex。"""
    if isinstance(c1, str):
        c1 = _hex_to_rgb(c1)
    if isinstance(c2, str):
        c2 = _hex_to_rgb(c2)
    l1, l2 = _rel_lum(c1), _rel_lum(c2)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def _ink_for_bg(bg, dark_ink="#101418", light_ink="#ffffff") -> str:
    """在 bg 上取深/浅两墨里对比度更高的那个（用于「强调底色上的文字」）。

    深墨取近黑 #101418 而非纯黑，纯黑在浅底上对比度略炸且发死；
    浅墨纯白。两者都按 WCAG 真算，择更优者，不写死「永远白/永远黑」。
    """
    return (dark_ink if _wcag_contrast(dark_ink, bg)
            >= _wcag_contrast(light_ink, bg) else light_ink)


def _on_accent_for(accent: str) -> str:
    """选中/强调底上的文字色：按强调色亮度取黑或白，**不编造**，真算对比度。

    本产品所有强调色（紫/蓝/青/钢灰，含深色模式的强调色）都偏亮，
    白字对比度都不足 4.5:1，故一律取深字；函数按亮度择更优墨色，
    未来若加深强调色会自动转白字，不写死。
    """
    return _ink_for_bg(accent)


def palette(dark: bool) -> dict:
    """自绘控件取色的唯一入口。

    强调色四档由**当前皮肤**决定（skin 换了强调色，QSS 与自绘同步跟着变，
    这正是 P1-4 要修的「皮肤半成品」）。柔和底/深文字三档同理。
    """
    base = dict(PALETTE["dark" if dark else "light"])
    ac, acd, ti, ho = _skin_colors(_CURRENT_SKIN)
    # R107：选中/强调底上的文字色按强调色亮度取黑或白（WCAG 真算），
    # 不再写死白字 —— 浅强调色（青/蓝/紫/钢灰）上白字对比度都不足 4.5:1。
    base["on_accent"] = _on_accent_for(ac)
    soft, soft_h, deep = SKIN_SOFT.get(_CURRENT_SKIN, SKIN_SOFT["violet"])
    tint_bg, tint_border, tint_title = SKIN_TINT_DARK.get(
        _CURRENT_SKIN, SKIN_TINT_DARK["violet"])
    base.update({"accent": ac, "accent_dim": acd,
                 "title": ti, "accent_hover": ho,
                 "accent_soft": soft, "accent_soft_hover": soft_h,
                 "accent_deep": deep,
                 # 深色下的「带强调色倾向」三档（选中底/描边/标题字）。
                 # 浅色下用同一组值就是错的（浅底浅字），所以按明暗取：
                 # 浅色 = SKIN_SOFT 那套，深色 = SKIN_TINT_DARK。
                 "tint_bg": tint_bg if dark else soft,
                 "tint_border": tint_border if dark else soft_h,
                 "tint_title": tint_title if dark else deep,
                 # ⚠ R128：「已完成」状态点**跟随皮肤**（P14b 契约：
                 #   `test_status_dot_color_follows_skin` 钉的就是这条），
                 #   但**真原生模式（classic）下退回语义中性色** ——
                 #   原生契约要求画面里**零**自绘强调色，连自己的都不行；
                 #   若这里还用 ac，classic 画面里就会钉一颗钢蓝点，
                 #   直接违反「原生模式完全交给系统样式画」。
                 #   跟随皮肤能真正生效还依赖另一半：TaskRow 的点图是
                 #   **建行那一刻烤进 QPixmap 的**，换肤必须重画（见
                 #   register_redraw / _redraw_registered），否则又会
                 #   变回「换到现代风，点还是上一套皮肤的紫」。
                 "st_done": (base["st_done"] if _NATIVE_MODE else ac),
                 # 「喜欢 / 排除」两个方向的语义色（看板双向条形图用）。
                 # pos 刻意**跟随皮肤**（喜欢就是产品的强调色方向），
                 # neg 是固定橙红 —— 它表达的是「另一侧」，跟随皮肤就失去意义了。
                 "pos": deep,
                 "track": base["border"]})
    return base


def c(dark: bool, key: str, fallback: str = "#888888") -> str:
    """`palette(dark)['accent']` 的短写，取不到就返回 fallback 而不是 KeyError。

    自绘代码里高频使用，且不该因为调色板加/删键就把整个绘制路径炸掉。
    """
    try:
        return palette(dark).get(key, fallback)
    except Exception:
        return fallback


# ══════════════════════════════════════════════════════════════════
# 行内 QSS 的取色入口（P1-4 第二步）
#
# 为什么不能继续写字面量：行内 `setStyleSheet("color:#5a6271")` 是**自带优先级**
# 的，QSS 换肤和 QPalette 都盖不住它 —— 换皮肤后这些控件纹丝不动，就是
# 「皮肤半成品」最直观的观感。
#
# 为什么用 `{key}` 占位而不是 f-string：f-string 要在**每个调用点**先把
# dark 传进来，一处漏传就静默变半深半浅；而 `style()` 记下模板、切换时
# 用**当前** dark 重渲，漏传不可能发生。
# ══════════════════════════════════════════════════════════════════
def css(dark: bool, template: str) -> str:
    """把 QSS 模板里的 `{key}` 换成调色板值。

    ⚠ R60：**真原生模式（classic）下直接返回空串** —— 行内 QSS 必须
      与全局 QSS 一起失效。行内 `setStyleSheet` 自带优先级，
      全局不套 QSS **盖不掉它**，漏这一条就是「按钮原生、任务列表
      选中态还是紫的」，比不改更割裂（NATIVE_SKIN_ORDER §3.3）。

    · `{accent}` / `{text}` / `{accent_soft}` … 语义色位；
    · `{ACCENT}` / `{TITLE_COLOR}` 这类**大写**的是本模块已有的常量；
    · **不认识的占位原样留下** —— QSS 本身满是花括号（`QPushButton{...}`），
      绝不能因为模板里出现花括号就炸掉整个绘制/布局路径。
    """
    if _NATIVE_MODE:
        # 原生模式：行内样式整体失效。**返回空串而不是 template** ——
        # 返回 template 会把模板里的 `{text}` 原样交给 Qt，
        # 那是非法 QSS，Qt 会**静默丢弃整段规则**（不报错），
        # 于是控件变成「什么都没发生」——比留个错色更难查。
        return ""
    try:
        pal = palette(dark)
    except Exception:
        return template
    out = []
    i, n = 0, len(template)
    while i < n:
        ch = template[i]
        if ch == "{":
            end = template.find("}", i + 1)
            if end != -1:
                key = template[i + 1:end]
                if key and " " not in key and ":" not in key:
                    if key in pal:
                        out.append(pal[key])
                        i = end + 1
                        continue
                    if key in globals():
                        out.append(str(globals()[key]))
                        i = end + 1
                        continue
        out.append(ch)
        i += 1
    return "".join(out)


# 行内样式登记表：(弱引用控件, 模板)。切换皮肤时统一重渲 —— 不登记的控件
# 换肤后仍是旧色。用 list + 弱引用而不是 dict：控件可能被销毁重建，
# 弱引用自动失效，不必谁负责从表里摘掉谁。
_INLINE_STYLES: list = []


def style(widget, dark, template: str = None):
    """给控件套一份**可随皮肤重渲**的行内样式。

    两种用法：
    · `style(w, dark, "color:{text}")`   —— 显式传深浅（构造期还不知道配置时）；
    · `style(w, "{text}")`               —— 省略 dark，由 `_CURRENT_DARK` 回答。

    刻意做成「建控件时调一次」的薄封装：调用点不需要知道自己会不会被换肤
    影响，也不需要在切换回调里补一份刷新清单（漏一处就是一个控件不跟随）。
    """
    if template is None:                   # style(w, "模板") 简写
        dark, template = _CURRENT_DARK, dark
    try:
        import weakref
        widget.setStyleSheet(css(dark, template))
        # ⚠ R60：原生模式下 css() 返 ""，所以上面那行等于清空样式；
        #   但**登记照留** —— 用户从 classic 切回 violet_look 时要靠它
        #   把模板按新皮肤重渲一遍。在这里 return 掉 / 不登记的话，
        #   切回去之后这些控件**永久裸奔**（见本文件头「坑2」）。
        _INLINE_STYLES.append((weakref.ref(widget), template))
    except Exception:
        pass
    return widget


def _restyle_registered(dark: bool):
    """切换深浅/皮肤后重渲所有登记过的行内样式。

    ⚠ R60：真原生模式下重渲的结果是**空串**（css() 在原生模式返 ""），
      此时**只清 QSS、保留登记**（alive 照收），这样切回非原生皮肤时
      这些控件还能被重新上色。若这里把登记丢了，切回去就是永久裸奔。
    """
    alive = []
    for ref, tpl in _INLINE_STYLES:
        w = ref()
        if w is None:
            continue                      # 控件已销毁，不必留
        try:
            w.setStyleSheet(css(dark, tpl))
            alive.append((ref, tpl))
        except Exception:
            pass                          # 已销毁/无 setStyleSheet，丢掉引用
    _INLINE_STYLES[:] = alive


def _reset_style_registry():
    """只给测试用：清空登记表（否则跨用例累积弱引用，量会失控）。"""
    _INLINE_STYLES.clear()


# ══════════════════════════════════════════════════════════════════
# 「烤进 QPixmap」的控件重绘登记（R128）
#
# QSS 换肤（unpolish/polish + _restyle_registered）**刷不动已经画好的
# QPixmap**：那张图的颜色是在画的那一刻取好、逐像素写进位图的。
# 任务行左侧的状态点就是这类：TaskRow 建行时 `dot.setPixmap(...)`，
# 之后无论怎么换皮肤，那颗点都还是建行时的颜色 —— 用户看到的就是
# 「切到现代风，'已完成'的点仍是紫罗兰」（R68 抓到的正是这个）。
#
# 做法与行内样式登记表同构：建控件时登记一个「重画自己」的回调，
# 换肤时统一跑一遍。不登记的控件换肤后仍是旧色（= 皮肤半成品）。
# ══════════════════════════════════════════════════════════════════
_REDRAW_HOOKS: list = []


def register_redraw(widget, fn) -> None:
    """登记「换肤时把 widget 重画一遍」的回调（fn 无参，内部自己取新色）。

    刻意做成建控件时调一次的薄封装，与 `style()` 同构：调用点不必
    知道自己会不会被换肤影响，也不必在切换回调里补刷新清单。
    """
    try:
        import weakref
        _REDRAW_HOOKS.append((weakref.ref(widget), fn))
    except Exception:
        pass


def _redraw_registered() -> None:
    """切换皮肤后让所有登记过的 pixmap 类控件重画。"""
    alive = []
    for ref, fn in _REDRAW_HOOKS:
        w = ref()
        if w is None:                       # 控件已销毁，不必留
            continue
        try:
            fn()
        except Exception:
            continue                        # 重画失败绝不能把换肤整个打断
        alive.append((ref, fn))
    _REDRAW_HOOKS[:] = alive


def _reset_redraw_registry():
    """只给测试用：清空重绘登记表。"""
    _REDRAW_HOOKS.clear()


# 当前皮肤（apply_theme 时更新，供 palette()/c() 取强调色用）。
# 不放进 SKINS 当参数层层传，是因为自绘点散落在十来个文件里，
# 让它们各自记得传 skin 迟早漏一个（漏一个就是那个控件不跟随皮肤）。
_CURRENT_SKIN = "violet"

# 当前深浅（apply_theme 时更新）。`style()` 不要求调用方传 dark ——
# 由这里统一回答，就不可能出现「某个控件忘了传 deep」。
_CURRENT_DARK = False

# ---- #43：水印穿透（表格页也要看得见水印）---------------------------------
# 根因：全局 `QWidget { background-color:#ffffff }`（深色模式是 #0f1115）是**通配
# 类型选择器**，stack 的各页、表格、表格 viewport / item 全被刷成实心底色，
# 而水印是画在 stack 上的（WatermarkOverlay 挂在 stack 并 lower() 到最底）——
# 一层实心子控件盖上去，水印就等于没画。
# 修法：把「水印到 stack 之间」这条链上的实心底全部拆掉，让水印透出来。
# stack **自身**的背景保留（它才是水印的画布底），只让 stack 的**后代**透明。
#   ① `QStackedWidget > QWidget` —— **子选择器（只命中直接子控件）**。
#      ⚠ 2026-10-06 真机 bug 修正：原写法是后代选择器 `QStackedWidget QWidget`，
#      当时以为"只命中 class 名恰为 QWidget 的对象、不会误伤 QPushButton" ——
#      **错了**。Qt 的 type selector 会连**子类**一起命中，所以它把 stack 里
#      所有后代（QPushButton / QLabel ...）都刷成了 transparent，而且
#      特异性 (0,0,2) 高过 `QPushButton{background-color:#f7a325}` 的 (0,0,1)，
#      于是按钮/标签全透明 —— 白底白字看不见，**鼠标悬停**才因
#      `QPushButton:hover`（伪类，特异性更高）重新显形。这就是台式机反馈的
#      「所有按钮/菜单/底部统计文字只有悬停才显示 + 淡色重影」的根因。
#      改成子选择器后只让 stack 的**直接子页**透明（水印才透得出来），
#      页面内部控件各自保留自己的底色，不再被误刷透明。
#   ② 表格整条链也要透明，否则表格页（列表/表格视图）右下角必遮水印。
# ⚠ 这是**普通字符串**（不是 f-string），所以大括号就写一个 `{` 一个 `}`，
#   千万别按 f-string 写成 `{{ }}` —— 那样 QSS 会收到两层括号，整段规则被判
#   语法错误**静默丢弃**（实测踩过：加了规则却一点没生效，还查不出为什么）。
_WM_PENETRATE = """
/* ---- #43 水印穿透 ---- */
/* ① **stack 自身**也必须透明。这条是实测踩出来的：WatermarkOverlay 挂 stack 后
      lower() 到子列表最底，只把"后代"刷透明是不够的 —— 只要 stack 自己还画一
      层实底（亮色 #ffffff / 深色 #0f1115），那层底就压在水印上面，水印照样
      看不见（实测：lower 后 stack grab 右下 (255,255,255)；raise_() 后同一块
      (78,78,255)。所以 stack 自己让位，底统统交给更上层容器）。
      ② stack 的**所有后代**也透明（只命中 class 名恰为 QWidget 的对象，
      不动 QPushButton / QLabel 这些本来就有自己规则的控件），页面内容因此是"透"的。
      ③ 表格整条链也透明，否则表格页右下角必遮水印。 */
QStackedWidget {
    background-color: transparent;
}
QStackedWidget > QWidget {
    background-color: transparent;
}
QTableWidget, QTableView,
QTableWidget::item, QTableView::item,
QTableWidget::viewport, QTableView::viewport {
    background-color: transparent;
}
QTableView::item:selected, QTableWidget::item:selected {
    background-color: #cfe0ff; color: #101418;
}
/* ④ 水印的落脚点：底部「日志/实时抓到」两块的**容器本身**透明，水印才透得
   出来；但它们的子控件（文字、缩略图）各自保留底色 —— 只点名这几个容器，
   不碰 QPushButton / QLabel 等其它控件，避免"整页透明"引发的重影与按钮消失。 */
#bottombar, #logstats, #logview, #livepanel, #livegrid {
    background-color: transparent;
}
/* ⑤ ⚠ 2026-10-06 真机 bug（"只露出 weet 半截"）的**真凶**，像素级实测定位：
      底部那块是 bottom_tabs（QTabWidget），全局 QSS 里它的 pane 被写成
      `QTabWidget::pane { border:none; background:#ffffff; }` —— **不透明白底**。
      而它右边界在窗口 x=1174，恰好把水印 logo（控件 1110..1370，内容带
      1120..1365）左边 54px 盖掉，于是 "Sweet" 的 S 没了，只剩 "weet"。

      实测记录（tools/_probe_r57_verify10.py，四组对照）：
        baseline            水印绘制 bbox x=1174..1365（宽 192，S 被裁）
        + pane transparent  水印绘制 bbox x=1120..1365（宽 246，S 完整）✅
      只加这一行就够，QTabWidget 自身与内部 stacked 不加也不挡（实测同值）。

      为什么以前没发现：#43 那轮把 #logstats/#logview/#livegrid 都点名透明了，
      唯独漏了它们**外面那层 QTabWidget 的 pane** —— 透明链断在最后一步。
      注意别顺手把 QTabWidget 整体也 transparent：图库/审核页签的 pane 需要
      实底做卡片分隔（见 DARK_QSS/LIGHT_QSS 里的 pane 规则），只放 ::pane 的
      background 即可，不动 border。 */
QTabWidget::pane {
    background: transparent;
}
"""

DARK_QSS = f"""
QWidget {{
    background-color: #0f1115; color: #e7eaf0; font-size: 13px;
    font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
}}
QLineEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background-color: #171a21; border: 1px solid #2a2f3a;
    border-radius: {RADIUS}; padding: 5px 8px;
    selection-background-color: {ACCENT_D};
}}
QLineEdit:focus, QTextEdit:focus, QComboBox:focus {{
    border: 1px solid {ACCENT_D};
}}
QListWidget {{
    background-color: #14171d; border: 1px solid #2a2f3a;
    border-radius: {RADIUS}; outline: 0;
}}
QListWidget::item {{ padding: 9px 7px; border-radius: 6px; }}
QListWidget::item:hover {{ background-color: rgba(91,140,255,0.14); }}
QListWidget::item:selected {{ background-color: {ACCENT_D}; color: __MRM_ON_ACCENT__; }}
QTableView::item:selected, QTableWidget::item:selected {{ background-color: {ACCENT_D}; color: __MRM_ON_ACCENT__; }}
QPushButton {{
    background-color: {ACCENT_D}; color: __MRM_ON_ACCENT__; border: none;
    border-radius: {RADIUS}; padding: 6px 16px; font-weight: 600;
}}
QPushButton:hover {{ background-color: #7c6cf0; }}
QPushButton:pressed {{ background-color: #6a5ce0; }}
QToolBar {{
    background: #14171d; border-bottom: 1px solid #2a2f3a;
    spacing: 6px; padding: 4px;
}}
QMenuBar {{ background: #14171d; }}
QMenuBar::item {{ padding: 4px 10px; border-radius: 6px; }}
QMenuBar::item:selected {{ background: #262c38; }}
QMenu {{ background: #171a21; border: 1px solid #2a2f3a; border-radius: {RADIUS}; }}
QMenu::item {{ padding: 6px 22px; border-radius: 6px; }}
QMenu::item:selected {{ background: {ACCENT_D}; color: __MRM_ON_ACCENT__; }}
QTabWidget::pane {{ border: none; background: #14171d; }}
QTabBar::tab {{
    background: #171a21; color: #aab1bd; padding: 8px 18px; margin-right: 3px;
    border-top-left-radius: {RADIUS}; border-top-right-radius: {RADIUS};
}}
QTabBar::tab:hover {{ color: #e7eaf0; }}
QTabBar::tab:selected {{
    background: #1c2029; color: #ffffff;
    border-bottom: 2px solid {ACCENT_D};
}}
QGroupBox {{
    border: 1px solid #2a2f3a; border-radius: {RADIUS};
    margin-top: 12px; background: #171a21;
}}
QGroupBox::title {{
    subcontrol-origin: margin; left: 12px; padding: 0 6px;
    color: #b0a6ff; font-weight: 700;
}}
QProgressBar {{
    border: 1px solid #2a2f3a; border-radius: {RADIUS};
    background: #171a21; text-align: center; height: 8px;
}}
QProgressBar::chunk {{ background-color: {ACCENT_D}; border-radius: {RADIUS}; }}
QSplitter {{ background: transparent; }}
QSplitter::handle {{ background-color: #2a2f3a; }}
QCheckBox {{ spacing: 6px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; }}
QMessageBox, QDialog, QInputDialog {{ background-color: #14171d; }}
QToolTip {{
    background-color: #1c2029; color: #e7eaf0;
    border: 1px solid #303643; border-radius: 6px;
}}
{_WM_PENETRATE}
/* ---- 迅雷式图标导航 ---- */
#navbtn {{
    background: transparent; color: #aab1bd; border: none;
    border-radius: 10px; font-size: 12px;
}}
#navbtn:hover {{ background: #1f242e; color: #e7eaf0; }}
/* R80：选中态从「淡紫底」改成**强调色实底 + 白字**，与 qB 分组导航的
   选中态（取 accent 键 + on_accent 字色）统一。
   动因：主单项3 撤掉任务页 4 个按钮后，modern 的画面里**一个强调色
   像素都不剩**（实测 0/1024×760）—— 换皮肤用户看不出来，正踩中
   test_r68「强调色因果」那条判据。导航选中态是默认视图里最大的一块
   可着色元素（56px 高 × 侧栏宽），拿来承担「当前在哪个页面」的指示
   也本来就是通行做法（qBittorrent 自己就是这么干的）。
   ⚠ 这段注释在 f-string 里，**不许出现花括号**（会被当占位符求值，
   上一版写了 键名 直接 NameError）。 */
#navbtn:checked {{ background: {ACCENT_D}; color: __MRM_ON_ACCENT__; font-weight: 600; }}

/* ---- WorkBuddy 风：左侧深色窄栏 ---- */
#sidebar {{ background: #171a21; border-right: 1px solid #262b34; }}
#titlebar {{ background: #171a21; border-bottom: 1px solid #262b34; }}
#sidebar QLabel {{ background: transparent; }}
#sidebar QListWidget {{ background: transparent; border: none; }}
#sidebar QListWidget::item {{ border: 1px solid transparent; }}
#sidebar QListWidget::item:hover {{ background: #1f242e;
    border: 1px solid #2a2f3a; }}
#sidebar QListWidget::item:selected {{ background: #2a2745; color: #e7eaf0; }}
QTabBar {{ background: transparent; }}
QTabBar::tab {{
    background: #1c2029; color: #aab1bd; border: 1px solid transparent;
    border-radius: 8px; margin-right: 4px; padding: 6px 18px; font-weight: 500;
}}
QTabBar::tab:hover {{ color: #e7eaf0; }}
QTabBar::tab:selected {{
    background: #171a21; color: #ffffff; font-weight: 600;
    border: 1px solid #4a4270;
}}

/* ---- R172：AI 模型卡片行 ----
   每档模型一张卡（名称+一句话优点 / 状态徽章 / 主按钮）。
   `mrmCurrent=1` 是「使用中」那一行：**描边 + 底纹**双通道标记，
   不靠颜色单通道 —— 5 皮肤里强调色差异很大，只换描边色在浅色皮肤上
   几乎看不出（R107 的老问题）。属性选择器写在全局 QSS 里，换肤时
   跟着 _recolor 一起换，不必在 Python 侧逐个重设。 */
QFrame#modelCard {{
    background: #171a21; border: 1px solid #2a2f3a; border-radius: 8px;
}}
QFrame#modelCard[mrmCurrent="1"] {{
    background: #2a2745; border: 1px solid #4a4270;
}}
"""

LIGHT_QSS = f"""
QWidget {{
    background-color: #ffffff; color: #1f2430; font-size: 13px;
    font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
}}
QLineEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background-color: #ffffff; border: 1px solid #d6dbe4;
    border-radius: {RADIUS}; padding: 5px 8px; color: #1f2430;
    selection-background-color: #cfe0ff;
}}
QLineEdit:focus, QTextEdit:focus, QComboBox:focus {{
    border: 1px solid {ACCENT};
}}
QListWidget {{
    background-color: #ffffff; border: 1px solid #d6dbe4;
    border-radius: {RADIUS}; outline: 0;
}}
QListWidget::item {{ padding: 9px 7px; border-radius: 6px; }}
QListWidget::item:hover {{ background-color: rgba(79,125,255,0.10); }}
QListWidget::item:selected {{ background: {ACCENT}; color: __MRM_ON_ACCENT__; }}
QTableView::item:selected, QTableWidget::item:selected {{ background: {ACCENT}; color: __MRM_ON_ACCENT__; }}
QPushButton {{
    /* 【SKIN_ORDER】原先这里是写死的 #f7a325（暖橙），**不随皮肤变** ——
       浅色下的强调色因此几乎看不见（实测：原始工程风与现代风抓图
       逐像素几乎相同，差异 8/48640），「皮肤」在浅色下等于半成品。
       深色 QSS 本来就是用 {ACCENT_D} 的，现在两边一致。 */
    background-color: {ACCENT}; color: __MRM_ON_ACCENT__; border: none;
    border-radius: {RADIUS}; padding: 6px 16px; font-weight: 600;
}}
QPushButton:hover {{ background-color: {TITLE_COLOR}; }}
QPushButton:pressed {{ background-color: {ACCENT_HOVER}; }}
QToolBar {{
    background: #ffffff; border-bottom: 1px solid #dde2ea;
    spacing: 6px; padding: 4px;
}}
QMenuBar {{ background: #ffffff; }}
QMenuBar::item {{ padding: 4px 10px; border-radius: 6px; }}
QMenuBar::item:selected {{ background: #eef1f6; }}
QMenu {{ background: #ffffff; border: 1px solid #d6dbe4; border-radius: {RADIUS}; }}
QMenu::item {{ padding: 6px 22px; border-radius: 6px; }}
QMenu::item:selected {{ background: {ACCENT}; color: __MRM_ON_ACCENT__; }}
QTabWidget::pane {{
    border: none; background: #ffffff;
}}
QTabBar::tab {{
    background: #e9edf3; color: #5a6271; padding: 8px 18px; margin-right: 3px;
    border-top-left-radius: {RADIUS}; border-top-right-radius: {RADIUS};
}}
QTabBar::tab:hover {{ color: #1f2430; }}
QTabBar::tab:selected {{
    background: #ffffff; color: #1f2430; font-weight: 600;
    border-bottom: 2px solid {ACCENT};
}}
QGroupBox {{
    border: 1px solid #d6dbe4; border-radius: {RADIUS};
    margin-top: 12px; background: #ffffff;
}}
QGroupBox::title {{
    subcontrol-origin: margin; left: 12px; padding: 0 6px;
    color: #7c6cf0; font-weight: 700;
}}
QProgressBar {{
    border: 1px solid #d6dbe4; border-radius: {RADIUS};
    background: #e9edf3; text-align: center; height: 8px;
}}
QProgressBar::chunk {{ background-color: {ACCENT}; border-radius: {RADIUS}; }}
QSplitter {{ background: transparent; }}
QSplitter::handle {{ background-color: #d6dbe4; }}
QCheckBox {{ spacing: 6px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; }}
QMessageBox, QDialog, QInputDialog {{ background-color: #ffffff; }}
QToolTip {{
    background-color: #1f2430; color: #ffffff;
    border: 1px solid #3b4456; border-radius: 6px;
}}
/* ---- 迅雷式图标导航 ---- */
#navbtn {{
    background: transparent; color: #5a6271; border: none;
    border-radius: 10px; font-size: 12px;
}}
#navbtn:hover {{ background: #f1f3f8; color: #1f2430; }}
/* R80：选中态改强调色实底 + 白字（与暗夜风/qB 导航同款，理由见 DARK_QSS
   同处注释）。⚠ 本段在 f-string 里，注释不许含花括号。 */
#navbtn:checked {{ background: {ACCENT}; color: __MRM_ON_ACCENT__; font-weight: 600; }}

/* ---- WorkBuddy 风：左侧浅灰窄栏 ---- */
#sidebar {{ background: #ffffff; border-right: 1px solid #eef0f4; }}
#titlebar {{ background: #ffffff; border-bottom: 1px solid #eef0f4; }}
#sidebar QLabel {{ background: transparent; }}
#sidebar QListWidget {{ background: transparent; border: none; }}
#sidebar QListWidget::item {{ border: 1px solid transparent; }}
#sidebar QListWidget::item:hover {{ background: #ffffff;
    border: 1px solid #e6e9f0; }}
#sidebar QListWidget::item:selected {{ background: #ecebfd; color: #1f2430; }}
/* ---- 胶囊标签页（选中=白色小卡片）---- */
QTabBar {{ background: transparent; }}
QTabBar::tab {{
    background: #f1f3f8; color: #5a6271; border: 1px solid transparent;
    border-radius: 8px; margin-right: 4px; padding: 6px 18px; font-weight: 500;
}}
QTabBar::tab:hover {{ color: #1f2430; }}
QTabBar::tab:selected {{
    background: #ffffff; color: #1f2430; font-weight: 600;
    border: 1px solid #dcd4f8;
}}
/* ---- R172：AI 模型卡片行（浅色侧，理由见 DARK_QSS 同段注释）---- */
QFrame#modelCard {{
    background: #ffffff; border: 1px solid #d6dbe4; border-radius: 8px;
}}
QFrame#modelCard[mrmCurrent="1"] {{
    background: #ecebfd; border: 1px solid #dcd4f8;
}}
{_WM_PENETRATE}
"""


def _arrow_assets(dark: bool) -> tuple[str, str]:
    """生成高对比度的上/下箭头 PNG（QSS 原生箭头太淡看不清）。

    返回 (down_url, up_url)，QSS 里的 file url 需要正斜杠路径。
    """
    from ..config import user_data_dir
    d = user_data_dir() / "theme_arrows"
    d.mkdir(parents=True, exist_ok=True)
    down, up = d / "down.png", d / "up.png"
    color = (235, 238, 245, 255) if dark else (55, 65, 85, 255)
    try:
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (14, 14), (0, 0, 0, 0))
        dr = ImageDraw.Draw(img)
        dr.polygon([(2, 4), (12, 4), (7, 11)], fill=color)      # down
        img.save(down)
        img2 = Image.new("RGBA", (14, 14), (0, 0, 0, 0))
        dr2 = ImageDraw.Draw(img2)
        dr2.polygon([(2, 10), (12, 10), (7, 3)], fill=color)    # up
        img2.save(up)
    except Exception:
        pass
    return down.as_posix(), up.as_posix()


def apply_preset(app, preset: str):
    """按**皮肤预设**应用外观（SKIN_ORDER 之后用户面对的唯一入口）。

    改造前用户要做两件事：先点「深色 / 浅色模式」，再去挑强调色。
    现在一个预设 = 一整套外观，这里是那「一件事」。

    ⚠ 为什么**保留** `apply_theme(dark, skin)` 不动：
      自绘控件与 P1-4 的护栏测试都是按 (dark, accent) 两参数调用的
      （`apply_theme(app, True, "violet"`），那是**取色引擎层**的入口，
      语义明确、也好测。`apply_preset` 是它上面的一层薄封装：
      预设 → (dark, accent) → apply_theme。删掉前者会一次性打断
      十几个测试点与所有自绘调用点，而它并没有做错什么。

    ⚠ R160：自定义皮肤在这里**翻译成「内建预设 + 一段追加 QSS」**
      —— 底下 apply_theme 只认 (dark, accent)，叠加层是这一层的额外信息，
      早翻译就能让取色/布局/日志全部走内建预设那一套现成口径。
    """
    global _CURRENT_PRESET
    _CURRENT_PRESET = resolve_preset_key(preset)
    dark = preset_dark(_CURRENT_PRESET)
    # ⚠ R60：native 由**这里**显式传下去（全模块唯一入口）。
    #   set_skin / 启动时读配置的 apply_preset 都会走到，
    #   而测试里那 43 处 apply_theme 直调保持 native=False（老行为）。
    apply_theme(app, dark, preset_accent(_CURRENT_PRESET),
                native=preset_is_native(_CURRENT_PRESET),
                extra_qss=custom_skin_qss(_CURRENT_PRESET))


def apply_theme(app, dark: bool, skin: str = None, native: bool = False,
                extra_qss: str = ""):
    """把外观刷到 app 上。

    `native=True`（R60）= **真原生 Windows**：只套水印穿透链那点 QSS、
    不设 QPalette（改回系统调色板）、行内样式一并失效、图标走系统。
    ⚠ 默认 False 且**不从 _CURRENT_PRESET 隐式推导** —— 见本文件头「坑1」。

    `extra_qss`（R160）= 自定义皮肤的叠加 QSS，**拼在整套样式最后**
      （CSS 后写者胜，用户那几行才能盖住内建的）。默认空串 = 老行为不变。

    ⚠ 原生模式（R60）下**照样套** —— 这一条是反复权衡后的选择：
      「真原生」约束的是**内建那个预设**（不许自绘），而自定义皮肤是用户
      **显式**选的额外一层，是他主动要的观感。若在原生下静默丢弃，用户
      基于原始工程风做的皮肤会「选了没反应」，正是本项目最忌讳的那类
      「看着像坏了」。底下的内建 QSS 仍然一条不套，原生底色没被污染。
    """
    from PySide6.QtGui import QColor, QPalette
    global _CURRENT_SKIN, _NATIVE_MODE
    _NATIVE_MODE = bool(native)
    _CURRENT_SKIN = skin or "violet"
    _ac, _acd, _ti, _ho = _skin_colors(skin)
    # P1-4：QPalette 用调色板取色，别再散写字面量 —— 皮肤切换后自绘控件
    # （缩略图底、网格边框、灯箱边框）也必须跟着重绘，见文件末尾。
    _pal = palette(dark)
    def _recolor(qss):
        """把 QSS 里写死的 violet 字面量换成**当前皮肤**的对应色。

        ⚠ 替换的是**源串**里的占位值，不是当前生效值。这里替换顺序很关键：
        `#8b7cf8` 换成的 `_ac` 可能恰好等于后面某个待替换的字面量
        （例如 violet→amber 时 `_ac=#f7a325`），如果链式replace 的下一段
        正好匹配到它，就会被再换一次 —— 所以四段替换的「新值」必须保证
        不会被后续段的「旧值」命中。下面用一个 dict 逐个一次性替换，
        避免链式替换的自污染。
        """
        # 强调色四档 + 柔和底/深文字三档（SKIN_SOFT）。
        # ⚠ 后三档原先**根本没进这张表**：浅色 QSS 的选中态底
        # （#ecebfd 侧栏选中、#dcd4f8 页签描边）写死淡紫，
        # 于是「换皮肤」在浅色下几乎看不见 —— 实测两个浅色皮肤
        # 抓图逐像素差异 8/48640。深色那侧当时是对得上的。
        _soft, _soft_h, _deep = SKIN_SOFT.get(_CURRENT_SKIN,
                                             SKIN_SOFT["violet"])
        _tb, _tbd, _tti = SKIN_TINT_DARK.get(_CURRENT_SKIN,
                                            SKIN_TINT_DARK["violet"])
        mapping = {
            "#8b7cf8": _ac, "#9d8cff": _acd, "#7c6cf0": _ti, "#7466e8": _ho,
            # 柔和底两档 + 一个「选中态描边」的近似值。
            # #efeaff/#e0d6ff 是 violet 的 soft/soft_hover（写死在浅色 QSS 里
            # 的是 #ecebfd/#dcd4f8 这两个同族淡紫，一并映射过去）。
            "#efeaff": _soft, "#e0d6ff": _soft_h,
            "#ecebfd": _soft, "#dcd4f8": _soft_h,
            # 深色侧的三个「带强调色倾向」色位（选中底/描边/分组标题字）。
            # 漏了它们的话，蓝皮肤在深色下选中项仍是一块紫 —— 换皮肤
            # 只换一半，比不换更难解释。
            "#2a2745": _tb, "#4a4270": _tbd, "#b0a6ff": _tti,
        }
        for old, new in mapping.items():
            qss = qss.replace(old, new)
        return qss
    down_url, up_url = _arrow_assets(dark)
    # 下拉框/微调框：明确的下拉区背景 + 清晰箭头（用户反馈原生箭头太不明显）
    arrows = f"""
QComboBox::drop-down {{
    border: none; width: 24px;
    background: {'#262c38' if dark else '#e9edf3'};
    border-top-right-radius: 8px; border-bottom-right-radius: 8px;
}}
QComboBox::down-arrow {{ image: url('{down_url}'); width: 12px; height: 12px; }}
QComboBox QAbstractItemView {{
    background: {'#171a21' if dark else '#ffffff'};
    border: 1px solid {'#2a2f3a' if dark else '#d6dbe4'};
    border-radius: 8px;
    selection-background-color: {ACCENT_D if dark else ACCENT};
    selection-color: __MRM_ON_ACCENT__; outline: 0;
}}
QComboBox QAbstractItemView::item {{
    background: {'#171a21' if dark else '#ffffff'};
    color: {'#e7eaf0' if dark else '#1f2430'};
    padding: 6px 10px; min-height: 22px; border: none;
}}
QComboBox QAbstractItemView::item:hover {{
    background: {ACCENT_D if dark else ACCENT}; color: __MRM_ON_ACCENT__;
}}
QComboBox QAbstractItemView::item:selected {{
    background: {ACCENT_D if dark else ACCENT}; color: __MRM_ON_ACCENT__;
}}
QComboBox QLineEdit {{ background: {'#171a21' if dark else '#ffffff'}; border: none; }}
QSpinBox, QDoubleSpinBox {{
    padding-right: 18px;
}}
QSpinBox::up-button, QDoubleSpinBox::up-button,
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    background: {'#262c38' if dark else '#e9edf3'}; border: none; width: 16px;
}}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{
    background: {ACCENT_D if dark else ACCENT};
}}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
    image: url('{up_url}'); width: 10px; height: 10px;
}}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
    image: url('{down_url}'); width: 10px; height: 10px;
}}
"""
    # R57：QSS 没变就别再喂给 Qt。`setStyleSheet` 即使字符串一模一样，也会把
    # 整张样式表对**每一个活着的控件**重新解析+应用一遍 —— 实测 300 个控件
    # 15.6ms、1500 个 77.2ms（线性），而 test_view_size.py 一个模块里就累积到
    # 5365 个控件，每次 boot 都白付~250ms。
    #
    # ⚠ 判据必须是「**新算出来的 QSS** vs「**app 现在身上带的**」，而不是
    # 「上次调用的皮肤名」：
    #   ① 比字符串 ⇒ 换皮肤/换深浅算出不同 QSS，照样重新应用（R68 钉死的
    #     「后来换皮肤仍是旧颜色」不会被放回来）；R57 探针实测 Qt 对我们这
    #     份 QSS 是**逐字节原样回读**的（5/5，含真实 7177 字符那份），所以
    #     不存在需要补偿的规范化。
    #   ② 读**活的状态**而不是自己的记忆 ⇒ 有一批测试会在 apply_theme 背后
    #     直接 `qapp.setStyleSheet("")`（test_r41:45,109,152,174、
    #     test_r47:43,115,139,172,229,262、test_r48_* 多处）。靠记忆的守卫
    #     会在这里误判成「已经上过色」而跳过 ⇒ app 永久无样式，且不报错。
    #     （护栏 tests/test_r57b_theme_early_return.py 钉死这两条。）
    # 只挡 setStyleSheet 这一个调用：下面的 setPalette / _restyle_registered /
    # restyle_themed_icons / unpolish-polish 扫描一个都不许省 —— 那是 P1-4
    # 「已显示的自绘控件也必须重绘」的全部依据。
    # ⚠ R60：原生模式**只**留 `_WM_PENETRATE`（水印穿透链）。
    #   这一条即使水印方案定稿后要改，也得留着 —— 它挡的是
    #   「全局 QWidget 实底把水印盖死」，删了水印会彻底消失（PART3 §3）。
    #   其余一律不套：自绘圆角/淡紫选中态/系统箭头全部交给 Windows 画。
    _qss = (_WM_PENETRATE if _NATIVE_MODE
            else _recolor(DARK_QSS if dark else LIGHT_QSS) + _recolor(arrows))
    # R160：自定义皮肤叠加在**最后**（CSS 后写者胜，用户那几行才盖得住内建）。
    # 空串是默认，老调用点行为逐字节不变。
    if extra_qss and extra_qss.strip():
        _qss = _qss + "\n" + extra_qss
    # R107：选中/强调底上的文字色（__MRM_ON_ACCENT__）按当前强调色亮度取黑或白，
    # 此处把占位符替成真值（调色板里已是 WCAG 达标色）。原生模式 _qss 无此占位，
    # 替换是空操作，安全。
    _qss = _qss.replace("__MRM_ON_ACCENT__", _pal["on_accent"])
    if app.styleSheet() != _qss:
        app.setStyleSheet(_qss)
    if _NATIVE_MODE:
        # ⚠ 必须**显式 reset**，不能只是「不设」（本文件头「坑3」）：
        #   用户先切暗夜风（设了深色 QPalette）再切原始工程风的话，
        #   跳过这一支会让深色调色板留着 —— QSS 原生 + 调色板深色 = 半原生。
        #   默认构造的 QPalette 就是**系统调色板**，正是我们要的。
        _sys_pal = QPalette()
        # R113：原生模式选中行沿用系统 Highlight 底色，但浅色 Windows 主题下
        #   系统 HighlightedText 是白字压中等钢灰（用户实测「灰底白字」，看不清
        #   任务编号/名称/网址/状态）。按系统 Highlight 真实亮度取对比度更高的墨色
        #   （WCAG 真算，不写死黑白），只覆盖这一个文字色位，不动原生外观。
        _hi = _sys_pal.color(QPalette.ColorRole.Highlight)
        _sys_pal.setColor(QPalette.ColorRole.HighlightedText,
                          QColor(_ink_for_bg(_hi.name())))
        # ⚠ R116：只 setPalette **一次**。
        #   改前是「先 setPalette 复位、改一个色位、再 setPalette」，
        #   即同一次 apply_theme 里连发两轮全应用级 PaletteChange 广播
        #   （每个已建控件都收 changeEvent → polish/unpolish）。
        #   全量 pytest 在原生模式连续跑 ~570 个用例后，第二轮广播会卡在
        #   Qt 原生调用里不返回（CPU 归零、内存不涨，非 Python 死锁，
        #   py-spy 显示主线程仍持 GIL 停在 apply_theme）。
        #   复位语义由这一次调用完整保留（默认构造的 QPalette 就是系统调色板）。
        app.setPalette(_sys_pal)
    elif dark:
        pal = QPalette()
        pal.setColor(QPalette.ColorRole.Window, QColor(_pal["bg"]))
        pal.setColor(QPalette.ColorRole.WindowText, QColor(_pal["text"]))
        pal.setColor(QPalette.ColorRole.Base, QColor(_pal["surface"]))
        pal.setColor(QPalette.ColorRole.Text, QColor(_pal["text"]))
        pal.setColor(QPalette.ColorRole.Button, QColor(_pal["surface_alt"]))
        pal.setColor(QPalette.ColorRole.ButtonText, QColor(_pal["text"]))
        pal.setColor(QPalette.ColorRole.Highlight, QColor(_acd))
        # R107：选中项文字色跟强调色亮度走（浅强调色→深字，WCAG AA ≥4.5）。
        pal.setColor(QPalette.ColorRole.HighlightedText,
                     QColor(_pal["on_accent"]))
        app.setPalette(pal)
    else:
        pal = QPalette()
        pal.setColor(QPalette.ColorRole.Window, QColor(_pal["bg"]))
        pal.setColor(QPalette.ColorRole.WindowText, QColor(_pal["text"]))
        pal.setColor(QPalette.ColorRole.Base, QColor(_pal["surface"]))
        pal.setColor(QPalette.ColorRole.Text, QColor(_pal["text"]))
        pal.setColor(QPalette.ColorRole.Button, QColor(_pal["surface_alt"]))
        pal.setColor(QPalette.ColorRole.ButtonText, QColor(_pal["text"]))
        pal.setColor(QPalette.ColorRole.Highlight, QColor(_ac))
        # R107：选中项文字色跟强调色亮度走（浅强调色→深字，WCAG AA ≥4.5）。
        pal.setColor(QPalette.ColorRole.HighlightedText,
                     QColor(_pal["on_accent"]))
        app.setPalette(pal)

    # P1-4 收尾：**已显示的自绘控件也必须重绘**。
    # 光换 QSS/QPalette 只影响「下次 paint 用到的样式」；那些已经缓存了
    # 画笔/画刷的自绘控件（缩略图背景、网格线、灯箱边框）不会自己变，
    # 用户看到的还是旧色 —— 这就是「皮肤是半成品」的另一半原因。
    # unpolish/polish 让 Qt 丢掉已缓存的样式解析，再 update() 强制重绘。
    global _CURRENT_DARK
    _CURRENT_DARK = bool(dark)
    if _NATIVE_MODE:
        # ⚠ R60：真原生恒浅色（系统观感）。写成「先按参数赋值、再在原生
        #   时纠正」而不是一行三元，是为了保住上面那行 `_CURRENT_DARK =
        #   bool(dark)` 的字面形态 —— tests/test_r67_inline_style_skin.py
        #   ::test_apply_theme_wires_restyle_and_icons 钉的就是这个串。
        #   （classic 在 PRESETS 里 dark=False，所以这条纠正平时不生效；
        #   但万一哪天 classic 被改成 dark=True，自绘控件会继续按深色
        #   取色，界面就半深半浅了。）
        _CURRENT_DARK = False
    # 行内 setStyleSheet 自带优先级，unpolish/polish **盖不住**它 —— 必须
    # 拿模板按新皮肤重渲一遍，否则换了皮肤这些控件还是老颜色。
    # R60：原生模式下这会把登记过的行内样式全部清空（但保留登记，见「坑2」）。
    _restyle_registered(bool(dark))
    # R128：烤进 QPixmap 的控件（任务行状态点等）QSS 刷不动，按登记重画。
    _redraw_registered()
    # 图标同理：QIcon 的颜色是构造时烤进 QPixmap 的，unpolish 刷不动它。
    try:
        from . import icons
        if _NATIVE_MODE:
            # R60 §3.4：原生模式不设自绘图标（自绘图标是"烤"进 QPixmap 的，
            # 颜色来自调色板，原生模式下那套配色本身就不该出现），
            # 按钮退回**纯文字**。不用 QStyle.StandardPixmap 是因为
            # v1 没有逐个控件挑系统图标的映射表，硬配反而会出现
            # 「同一个动作在两处是不同图标」这种更糟的情况。
            icons.clear_themed_icons()
        else:
            icons.restyle_themed_icons()
    except Exception:
        pass
    try:
        for w in app.topLevelWidgets():
            if not w.isVisible():
                continue
            w.style().unpolish(w)
            w.style().polish(w)
            w.update()
    except Exception:
        pass
    app.processEvents()