"""r238 护栏：启动页用 logo + 转圈（网页）；桌面闪屏用 logo（找不到则退回文字）。

启动页是最容易被改坏又最难发现的地方 —— 它只在**启动那一两秒**出现：
- 图裂了 / 路径错了 → 用户看到的是一块空白，而且刷新一下就好了，根本不会来报；
- 文字被删了、logo 又没画上 → 桌面端闪屏就是一块纯色板，像卡死；
- 动画开关关掉后图标消失 → 启动页什么都不动，以为没启动成功。

所以三条都要用判据钉住。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "media_resource_manager/webapp/src"


def _read(rel: str) -> str:
    return (SRC / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------- 网页启动页

def test_boot_screen_is_logo_not_text():
    a = _read("App.vue")
    assert 'class="boot-logo"' in a
    assert ':src="logo"' in a
    # ⚠ 只看 boot 那个 div 的**内容**，不看整文件：注释里解释了"原来写的是
    #   「正在连接…」"，整文件搜会把「解释为什么改」当成「没改」（第一次就栽这）。
    seg = a.split('<div v-if="!ready" class="boot">', 1)[1].split("</div>", 1)[0]
    assert "正在连接" not in seg, "启动页还在用文字（用户要求换成 logo）"


def test_boot_screen_has_spinner():
    a = _read("App.vue")
    # anim="spin" 是 svg-spinners:180-ring；关掉动画时退回 name="loader"（静态圆环）
    assert 'anim="spin"' in a and 'name="loader"' in a
    assert 'class="boot-spin"' in a


def test_boot_logo_survives_missing_asset():
    """logo 取不到时不能留个破图图标 —— 先退回原图，再失败就隐藏，只留转圈。

    V0.0.27 改判（不是放宽）：启动页改用裁掉透明边的字标 `/brand/wordmark.png`，
    老版本后端没有这条路由 ⇒ 以前那行内联 `onerror` 直接隐藏，字标就此消失；
    现在统一走 `onLogoError`：先退回 `/brand/logo.png`，再失败才隐藏。
    """
    a = _read("App.vue")
    seg = a.split('class="boot-logo"', 1)[1].split("/>", 1)[0]
    assert '@error="onLogoError"' in seg
    assert "function onLogoError" in a
    assert "logoFallback" in a and "display = 'none'" in a


def test_boot_uses_backend_logo_route():
    """走 /brand/logo.png：这样用户换了 logo 不用重新打包前端。"""
    a = _read("App.vue")
    assert "/brand/logo.png" in a


def test_boot_layout_centers_both():
    a = _read("App.vue")
    seg = a.split(".boot {", 1)[1].split("}", 1)[0]
    assert "flex" in seg and "column" in seg
    assert "justify-content: center" in seg
    assert ".boot-logo" in a and ".boot-spin" in a


# ---------------------------------------------------------------- 桌面闪屏

def _splash_src() -> str:
    # 2026-10-29（r257）：启动条件多了「进托盘就不画闪屏」，切片关键词随之改成
    # `if (bool(getattr(_cfg, "splash_enabled", True))` —— 被切的还是同一段代码。
    src = (ROOT / "media_resource_manager/gui/main_window.py").read_text(encoding="utf-8")
    return src.split('if (bool(getattr(_cfg, "splash_enabled", True))', 1)[1] \
              .split("win = MainWindow()", 1)[0]


def test_desktop_splash_draws_logo():
    seg = _splash_src()
    assert "logo_path" in seg, "桌面闪屏没去取 logo"
    assert "drawPixmap" in seg
    assert "SmoothTransformation" in seg, "放大 logo 不平滑会很糊"


def test_desktop_splash_keeps_text_fallback():
    """取不到 logo 时必须退回文字 —— 纯色板闪屏比没闪屏更让人以为崩了。"""
    seg = _splash_src()
    assert "_drew_logo" in seg
    assert "if not _drew_logo:" in seg
    assert "Sweet 采集AI版" in seg
    assert "正在启动…" in seg


def test_desktop_splash_no_hardcoded_colors():
    """p14b 只扫 hex，但把颜色写成 rgb() 一样是硬编码；这里守住 theme.c()。

    ⚠ 判据不能直接搜 `#` —— Python 注释也是 `#`，一加注释就整条红。
      要看的是"颜色字面量"：`QColor("#xxxxxx")` / `fill(QColor(0x...)` 这类。
      注释行先剥掉，再匹配带引号的 hex。
    """
    import re
    seg = _splash_src()
    # 2026-10-29（用户第 16 条）：底色从「深色调色板」改成「浅色调色板」——
    # 原来 c(True,…) 取的是深色 bg，所以不管什么皮肤启动画面都是黑底。
    assert "QColor(_th.c(False" in seg
    code = "\n".join(ln for ln in seg.splitlines()
                      if not ln.lstrip().startswith("#"))
    bad = re.findall(r'[\'"]((?:#|0x)[0-9a-fA-F]{3,8})[\'"]', code)
    assert not bad, f"闪屏里有写死的颜色：{bad}"


def test_splash_logo_failure_does_not_crash_startup():
    """logo 读取整段包在 try 里：读图失败绝不能把整个启动流程带崩。"""
    seg = _splash_src()
    before = seg.split("_drew_logo = False", 1)[1].split("if not _drew_logo:", 1)[0]
    assert "try:" in before and "except Exception:" in before
