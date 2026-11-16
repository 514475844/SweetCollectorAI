"""⑧ 自动编写适配器：给定网址 -> 启发式产出抓图规则（选择器 + 翻页）。

卡点（已定案）：软件里只有视觉 CLIP，**没有文本生成模型**，所以这里不做
"AI 猜选择器"，而是用**纯启发式**从页面 HTML 里提取图片候选 / 翻页模式，
生成一份**待人工确认的候选 AdapterRule**。

设计约束（来自作业单）：
  * 生成后**先预览、确认才落 yaml**，不许自动入库；
  * 落盘的是普通 yaml（进 data/rules），**纳入 check_rules_drift，不许免检**；
  * 入口＝「网址与适配器管理器…」里加按钮，**不新开**「维护 → 自动编写适配器…」。

核心 `propose_adapter(url, html)` 是**纯函数**（不联网），便于单测；
联网抓取由 UI 层的 `fetch_url()` 单独负责，测试里直接 monkeypatch。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import yaml

# Qt 依赖放在**模块级**：_BusyOverlay / _AnalyzeWorker 是模块级类，
# 而下面的 build_auto_gen_dialog 里那些 import 是函数内的 —— 类定义在
# 函数 import 之前就执行了，会直接 NameError: name 'QWidget' is not defined。
# （第一版把 import 全写在函数里，测试一跑就暴露了这个坑。）
# R253：Qt 改成**容错导入**。这个模块里的纯函数（propose_adapter 等）现在
# 也被网页端 / 无窗口服务用到，而那些场景没必要装 Qt —— 模块级硬 import
# 会让「只想用启发式生成规则」也拖进一整套 Qt 运行库（Docker 镜像 +300MB）。
# 只有桌面端的对话框函数真正需要 Qt；缺了就在调用处报一句人话。
try:                                   # pragma: no cover - 环境相关
    from PySide6.QtCore import Qt, QThread, QTimer, Signal, QObject, QEvent
    from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QLineEdit,
                                   QPlainTextEdit, QPushButton, QVBoxLayout,
                                   QWidget, QMessageBox)
    HAS_QT = True
except Exception:                      # noqa: BLE001
    HAS_QT = False

    class QWidget:                     # type: ignore[no-redef]
        """占位：桌面端缺 Qt 时类定义仍能执行（只有实例化才会失败）。"""
        def __init__(self, *a, **k):
            raise RuntimeError("自动编写适配器的图形界面需要桌面端环境")

    class _Missing:
        def __getattr__(self, name):
            raise RuntimeError("自动编写适配器的图形界面需要桌面端环境")

    QDialog = QLabel = QPushButton = QLineEdit = QPlainTextEdit = _Missing()  # type: ignore
    QThread = QObject = QTimer = _Missing()                                  # type: ignore
    QHBoxLayout = QVBoxLayout = _Missing()                                   # type: ignore
    Signal = Qt = QEvent = _Missing()                                        # type: ignore
    QMessageBox = _Missing()                                                 # type: ignore

from ..utils.url import domain_of
from .rules import AdapterRule

IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")
NEXT_KEYWORDS = ("下一页", "下一頁", "next", "next page", "»", "›", ">", "older")


def _extract_links(html: str) -> list[tuple[str, str]]:
    """返回 [(href, 文本)]，文本已去标签。"""
    out: list[tuple[str, str]] = []
    for m in re.finditer(r'<a\b[^>]*\bhref=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                         html, re.I | re.S):
        href = m.group(1).strip()
        text = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        out.append((href, text))
    return out


def _extract_imgs(html: str) -> list[str]:
    """返回 img 标签里首个可用的 src/data-src/data-original。"""
    out: list[str] = []
    for m in re.finditer(r"<img\b[^>]*>", html, re.I):
        tag = m.group(0)
        for attr in ("data-src", "data-original", "src"):
            mm = re.search(rf'{attr}=["\']([^"\']+)["\']', tag, re.I)
            if mm:
                out.append(mm.group(1).strip())
                break
    return out


def _safe_name(domain: str) -> str:
    return domain or "auto-adapter"


def propose_adapter(url: str, html: str = "") -> tuple[AdapterRule, dict]:
    """纯函数：根据网址 + 页面 HTML 提出一份候选适配器规则。

    返回 (AdapterRule, stats)。无任何网络 / 模型依赖，便于离线单测。
    html 为空（没抓到页面）时回退到「通用 img 选择器模板」，仍要求人工确认。
    """
    url = (url or "").strip()
    domain = domain_of(url)
    stats: dict = {
        "domain": domain,
        "img_tags": 0,
        "direct_image_links": 0,
        "album_like_links": 0,
        "pagination_detected": False,
        "pagination_expr": "",
        "note": "",
    }

    if not html:
        stats["note"] = "未抓取到页面 HTML，使用通用 img 选择器模板，请人工在编辑器确认。"
        rule = _build_rule(_safe_name(domain), url, domain, img_direct=0,
                           pagination=None)
        return rule, stats

    links = _extract_links(html)
    imgs = _extract_imgs(html)
    stats["img_tags"] = len(imgs)
    stats["direct_image_links"] = sum(
        1 for href, _ in links if href.lower().split("?")[0].endswith(IMG_EXT))

    album_like = 0
    pagination = None
    for href, text in links:
        low = (href + " " + text).lower()
        if re.search(r"/(album|gallery|photo|post|show|item|detail|thread)/\d*",
                     href, re.I):
            album_like += 1
        if pagination is None and _looks_like_next(href, text):
            pagination = href
    stats["album_like_links"] = album_like
    if pagination:
        stats["pagination_detected"] = True
        stats["pagination_expr"] = (
            "a:contains(下一页), a[rel=next], a.next, a[class*=next], "
            "a[href*=page], a[href*=p=]")
        stats["note"] = "检测到翻页入口，已生成候选 next_page 选择器；请人工核对。"
    else:
        stats["note"] = "未检测到明显翻页入口，默认不翻页；如需翻页请人工补充。"

    rule = _build_rule(_safe_name(domain), url, domain,
                       img_direct=stats["direct_image_links"],
                       pagination=pagination)
    return rule, stats


def _looks_like_next(href: str, text: str) -> bool:
    t = text.strip().lower()
    h = href.lower()
    if t in NEXT_KEYWORDS:
        return True
    if re.search(r'<a\b[^>]*\brel=["\']?next', "", re.I) and "next" in h:
        return True
    if re.search(r"(page|p)=", h) and any(c.isdigit() for c in h):
        return True
    if re.search(r"/page/\d+", h):
        return True
    return False


def _build_rule(name: str, url: str, domain: str,
                img_direct: int, pagination: Optional[str]) -> AdapterRule:
    image_link_selectors = [
        {"kind": "css", "expr": "img", "attr": "src"},
        {"kind": "css", "expr": "img", "attr": "data-src"},
        {"kind": "css", "expr": "img", "attr": "data-original"},
    ]
    # 有直接图片链接时，优先抓 a[href=图片] 这种直链，比盲目扫 img 更准。
    if img_direct:
        image_link_selectors.insert(0, {
            "kind": "css",
            "expr": ("a[href$=.jpg], a[href$=.jpeg], a[href$=.png], "
                     "a[href$=.webp], a[href$=.gif]"),
            "attr": "href",
        })
    next_page_selectors = {}
    if pagination:
        next_page_selectors = {
            "kind": "css",
            "expr": ("a:contains(下一页), a[rel=next], a.next, "
                     "a[class*=next], a[href*=page], a[href*=p=]"),
        }
    return AdapterRule(
        name=name,
        base_url=url or f"https://{domain}",
        allowed_domains=[domain] if domain else [],
        image_link_selectors=image_link_selectors,
        next_page_selectors=next_page_selectors,
        max_depth=2,
        max_pages=0,
        delay=1.0,
        concurrency=4,
        note="自动分析生成（启发式，未使用文本模型）；选择器与翻页需人工确认后再用。",
    )


def dump_rule_yaml(rule: AdapterRule) -> str:
    """把 AdapterRule 序列化成 yaml 字符串（含中文可读）。"""
    data = {
        "name": rule.name,
        "base_url": rule.base_url,
        "allowed_domains": rule.allowed_domains,
        "image_link_selectors": rule.image_link_selectors,
        "next_page_selectors": rule.next_page_selectors,
        "max_depth": rule.max_depth,
        "max_pages": rule.max_pages,
        "delay": rule.delay,
        "concurrency": rule.concurrency,
        "note": rule.note,
    }
    header = (
        "# 自动分析生成的适配器（启发式，未使用文本模型）。\n"
        "# 使用前请人工在「打开编辑」里核对选择器与翻页是否匹配该站点。\n")
    return header + yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


def propose_filename(rules_dir: Path, domain: str) -> str:
    """给定规则目录与域名，返回一个不冲突的 yaml 文件名（不含目录）。"""
    base = _safe_name(domain) or "auto-adapter"
    base = "".join(c for c in base if c.isalnum() or c in "-_.")
    if not base:
        base = "auto-adapter"
    candidate = f"{base}.yaml"
    n = 2
    while (Path(rules_dir) / candidate).exists():
        candidate = f"{base}-{n}.yaml"
        n += 1
    return candidate


def fetch_url(url: str, timeout: float = 15.0, proxy: Optional[str] = None) -> str:
    """联网抓取页面 HTML（UI 层用；测试可 monkeypatch）。"""
    import httpx
    r = httpx.get(url, proxy=proxy or None, timeout=timeout,
                  follow_redirects=True,
                  headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return r.text


# --------------------------------------------------------------------------
# UI：预览对话框（确认才落盘）
# --------------------------------------------------------------------------
class _BusyOverlay(QWidget):
    """分析期间的「转圈」等待遮罩（C2）。

    铁律：耗时操作必须在后台线程跑，UI 主线程绝不阻塞。这里就是给主线程
    放一个转圈动画盖在对话框上，让用户知道"在动、别关"。
    纯QWidget + QTimer，不碰事件循环（r38 约束：禁 create_subprocess、GUI 不 pump）。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("busyOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            "#busyOverlay { background: rgba(255,255,255,0.82); }")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addStretch()
        self._dots = QLabel("正在分析…")
        self._dots.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # P1-4：文字色原先写死（#7c6cf0 是 violet 专属标题色 / #a0a6b3 灰）。
        # 走 theme.css() 解析 `{key}` 模板：跟着当前皮肤与深浅走。
        # ⚠ 这个模块在 crawler/ 下却要用 gui/theme —— 是有意的：遮罩是纯 UI
        # 部件，颜色语义属于主题层。放在函数内 import 是为了让本模块的
        # **纯逻辑部分**（propose_adapter）仍然不依赖 Qt/主题。
        from ..gui.theme import style as _tstyle
        _tstyle(self._dots,
                "font-size:16px; font-weight:700; color:{title};"
                "background:transparent;")
        lay.addWidget(self._dots)
        self._hint = QLabel("（抓页面 + 猜选择器，通常几秒）")
        self._hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        _tstyle(self._hint,
                "font-size:12px; color:{text_dim}; background:transparent;")
        lay.addWidget(self._hint)
        self._hint2 = QLabel("")   # 第二行提示（可动态改文案）
        self._hint2.setAlignment(Qt.AlignmentFlag.AlignCenter)
        _tstyle(self._hint2,
                "font-size:12px; color:{text_dim}; background:transparent;")
        lay.addWidget(self._hint2)
        lay.addStretch()
        # 转圈：定时器只改文字里的点号个数。**不**用 QMovie 读 GIF 资源，
        # 免得打包时少一个文件就不转了（也省一次解码）。
        self._phase = 0
        self._timer = QTimer(self)
        self._timer.setInterval(350)
        self._timer.timeout.connect(self._spin)

    def _spin(self):
        self._phase = (self._phase + 1) % 4
        self._dots.setText("正在分析" + "." * self._phase)

    def start(self, hint: str = ""):
        if hint:
            self._hint2.setText(hint)
        self._phase = 0
        self._spin()
        self._timer.start()
        self.show()
        self.raise_()
        self.updateGeometry()

    def stop(self):
        self._timer.stop()
        self.hide()


class _AnalyzeWorker(QThread):
    """后台分析：抓页面 + 猜选择器 + 出规则（C2）。

    结果通过 ``done`` 信号回主线程。**绝不在这里碰 UI**（跨线程动控件会崩）。
    """

    done = Signal(object, object, object, str)   # rule, stats, fname, warn

    def __init__(self, url: str, rules_dir: Path, timeout: float = 20.0):
        super().__init__()
        self.url = url
        self.rules_dir = Path(rules_dir)
        self.timeout = timeout

    def run(self):
        warn = ""
        html = ""
        try:
            html = fetch_url(self.url, timeout=self.timeout)
        except Exception as e:
            # 联网失败不致命：回退模板 + 在预览里说明为什么。
            warn = (f"[WARN] 抓取失败（{type(e).__name__}: {e}），"
                    f"改用通用模板。")
        rule, stats = propose_adapter(self.url, html)
        fname = propose_filename(self.rules_dir, stats["domain"])
        self.done.emit(rule, stats, fname, warn)


def build_auto_gen_dialog(parent, rules_dir: Path, on_saved=None) -> "object":
    """构建「分析这个网址 / 自动生成规则」对话框（不 exec，便于单测）。

    C4 重构后的结构（**不再要求先在上一层建好规则再进来**）::

        名称[____________]网址[____________][分析]
        ──────────────── 预览区 ────────────────
                                    [确定] [关闭]

    C2：点「分析」只启动后台线程 + 显示转圈遮罩，主线程立刻返回事件循环；
        分析完由 worker 的 done 信号回主线程刷预览、收遮罩。
        **绝不能**在主线程里直接 fetch_url —— 那是同步 httpx.get，
        8~20 秒的阻塞会让整个窗口"卡死"（用户原话：分析就卡死了）。

    C3：「确定」写盘后**关闭对话框**并弹「已保存」提示，且**读回校验**，
        确认真的落到了 rules_dir 下。
    """
    # Qt 的 import 在**模块顶部**（_BusyOverlay / _AnalyzeWorker 是模块级类，
    # 必须在 import 之后才能定义）。这里不再重复 import。
    dlg = QDialog(parent)
    dlg.setWindowTitle("分析这个网址 / 自动生成规则")
    dlg.resize(660, 520)
    v = QVBoxLayout(dlg)
    v.addWidget(QLabel(
        "填名称 + 网址，自动分析页面结构 → 预览候选规则"
        "（域名 / 选择器 / 抓到几张 / 翻页方式）。\n"
        "点「确定」才会写进适配器目录，不会自动入库。"))

    # ---- C4：名称 + 网址 + 分析 ----
    url_row = QHBoxLayout()
    name_in = QLineEdit()
    name_in.setPlaceholderText("规则名称（如 bilibili）")
    url_in = QLineEdit()
    url_in.setPlaceholderText("https://...")
    b_analyze = QPushButton("分析")
    url_row.addWidget(name_in, 1)
    url_row.addWidget(url_in, 2)
    url_row.addWidget(b_analyze)
    v.addLayout(url_row)

    preview = QPlainTextEdit()
    preview.setReadOnly(True)
    v.addWidget(preview, 1)

    b_confirm = QPushButton("确定")
    b_confirm.setEnabled(False)
    b_close = QPushButton("关闭")
    h = QHBoxLayout()
    h.addStretch()
    h.addWidget(b_confirm)
    h.addWidget(b_close)
    v.addLayout(h)

    state: dict = {"rule": None, "fname": None, "worker": None}

    def _overlay():
        """懒建遮罩（盖在对话框上，跟随窗口尺寸）。"""
        ov = getattr(dlg, "_overlay", None)
        if ov is None:
            ov = _BusyOverlay(dlg)
            dlg._overlay = ov
            ov.resize(dlg.size())
        return ov

    class _Filter(QObject):
        """对话框 resize 时同步遮罩尺寸（否则遮罩不跟着变形、露边）。"""
        def eventFilter(self, obj, ev):
            if ev.type() == QEvent.Type.Resize and getattr(obj, "_overlay", None):
                obj._overlay.resize(obj.size())
            return False

    filt = _Filter(dlg)
    dlg.installEventFilter(filt)
    dlg._filter = filt            # 防被GC 掉（装上事件过滤器后对象必须活着）

    def set_busy(busy: bool, hint: str = ""):
        b_analyze.setEnabled(not busy)
        b_analyze.setText("分析中…" if busy else "分析")
        b_confirm.setEnabled((not busy) and state.get("rule") is not None)
        if busy:
            _overlay().start(hint)
        else:
            ov = getattr(dlg, "_overlay", None)
            if ov is not None:
                ov.stop()

    def render_preview(rule, stats, fname, warn):
        lines = []
        if warn:
            lines.append(warn)
        lines += [
            "—— 预览候选规则 ——",
            f"域名：{stats['domain']}",
            f"候选图片：img 标签 {stats['img_tags']} 个 / 直链图片 "
            f"{stats['direct_image_links']} 个",
            f"翻页方式：{'已检测（见 next_page_selectors）' if stats['pagination_detected'] else '未检测（默认不翻页）'}",
            f"落盘文件名：{fname}",
            "",
            "===== YAML =====",
            dump_rule_yaml(rule),
            "—— 说明 ——",
            stats["note"],
        ]
        preview.setPlainText("\n".join(lines))

    def do_analyze():
        """启动后台分析（C2：主线程只发信号，绝不阻塞）。"""
        url = url_in.text().strip()
        if not url.startswith(("http://", "https://")):
            QMessageBox.warning(dlg, "网址无效", "请填写以 http(s):// 开头的网址。")
            return
        # 旧 worker 还在跑就先中止并等它（连点「分析」不应叠出两个线程）。
        old = state.get("worker")
        if old is not None:
            try:
                old.requestInterruption()
                if old.isRunning() and not old.wait(1500):
                    old.terminate()
                    old.wait(500)
            except Exception:
                pass
        name = name_in.text().strip()
        set_busy(True, f"目标：{name or '（名称将自动生成）'}")

        def _on_done(rule, stats, fname, warn):
            # 回到主线程：刷预览、收遮罩、按用户填的名称覆盖文件名。
            if name:
                safe = "".join(c for c in name if c.isalnum() or c in "-_")
                if safe:
                    fname = f"{safe}.yaml"
            state["rule"] = rule
            state["fname"] = fname
            render_preview(rule, stats, fname, warn)
            set_busy(False)

        def _on_finished():
            state["worker"] = None
            set_busy(False)

        w = _AnalyzeWorker(url, Path(rules_dir))
        w.done.connect(_on_done)
        w.finished.connect(_on_finished)
        state["worker"] = w
        w.start()

    def do_confirm():
        """C3：写盘 → **读回校验** → 关闭对话框 + 提示「已保存」。"""
        rule = state.get("rule")
        fname = state.get("fname")
        if rule is None or not fname:
            return
        dest = Path(rules_dir) / fname
        try:
            dest.write_text(dump_rule_yaml(rule), encoding="utf-8")
        except Exception as e:
            QMessageBox.critical(dlg, "保存失败",
                                 f"写入失败：{type(e).__name__}: {e}")
            return
        # 读回校验：写完不检查等于没保证 —— 真出问题时用户已经关窗了，
        # 下次用规则时才发现「我明明保存了」，那时候根本想不起是哪一步坏的。
        try:
            back = dest.read_text(encoding="utf-8")
            if not back.strip() or "name:" not in back:
                QMessageBox.critical(
                    dlg, "保存失败",
                    f"已写入 {dest}，但读回校验不通过（文件内容异常）。")
                return
            n_rules = len(list(Path(rules_dir).glob("*.yaml"))) \
                + len(list(Path(rules_dir).glob("*.yml")))
        except Exception as e:
            QMessageBox.critical(dlg, "保存失败",
                                 f"写入 {dest} 后读回失败：{type(e).__name__}: {e}")
            return
        b_confirm.setEnabled(False)
        preview.appendPlainText(f"\n[OK] 已保存：{dest}")
        if callable(on_saved):
            try:
                on_saved()
            except Exception:
                pass
        # 先关窗再弹提示：弹窗是模态的，留在本窗上会让人以为还在编辑。
        dlg.accept()
        QMessageBox.information(
            None, "已保存",
            f"适配器规则已保存：\n{dest}\n\n"
            f"当前规则目录共 {n_rules} 份规则。")

    b_analyze.clicked.connect(do_analyze)
    url_in.returnPressed.connect(do_analyze)
    b_confirm.clicked.connect(do_confirm)
    b_close.clicked.connect(dlg.reject)

    # 暴露给单测
    dlg._name_in = name_in
    dlg._url_in = url_in
    dlg._preview = preview
    dlg._b_confirm = b_confirm
    dlg._b_analyze = b_analyze
    dlg._b_close = b_close
    dlg._do_analyze = do_analyze
    dlg._do_confirm = do_confirm
    dlg._state = state
    dlg._rules_dir = Path(rules_dir)
    dlg._on_saved = on_saved
    return dlg


def open_auto_gen(parent, rules_dir: Path, on_saved=None) -> None:
    """入口：在「网址与适配器管理器…」的适配器 Tab 加按钮后调用。"""
    dlg = build_auto_gen_dialog(parent, rules_dir, on_saved)
    try:
        dlg.exec()
    finally:
        # 对话框关掉时务必把后台线程收干净：QThread 在 running 状态被析构
        # 会让 Qt 直接 abort 整个进程（= 用户说的「一点分析软件就退出」）。
        w = dlg._state.get("worker")
        if w is not None:
            try:
                w.requestInterruption()
                if w.isRunning() and not w.wait(3000):
                    w.terminate()
                    w.wait(1000)
            except Exception:
                pass
            dlg._state["worker"] = None
