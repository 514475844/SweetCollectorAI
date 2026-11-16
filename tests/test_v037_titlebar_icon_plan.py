"""V0.0.37 三件：用户母版图标、自绘标题栏（彻底关闭＋缩小到托盘）、限速时段表。

用户原话（口径）：
  · exe 图标、窗口左上角图标用「我给你的图」；
  · 「右上角的关闭是最小化到托盘了」→ 关闭改彻底关闭，最大化旁边加缩小到托盘；
  · 限速时段表「怎么又不对啦」→ 根因是网页 time 控件装不下 24:00，
    且开关离表格太远、配了表没开开关看不出来。

判据钉关系，不钉死文案字节数。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "media_resource_manager"
WEB = SRC / "webapp" / "src"


def _read(rel: str) -> str:
    p = ROOT / rel
    assert p.is_file(), f"缺文件 {rel}"
    return p.read_text(encoding="utf-8")


def _web(rel: str) -> str:
    p = WEB / rel
    assert p.is_file(), f"缺文件 {rel}"
    return p.read_text(encoding="utf-8")


# ───────────────────── ① 图标：exe / 窗口用用户给的图 ─────────────────────
def test_icon_is_the_sidebar_wordmark():
    """V0.0.38 改判：用户要的是**侧栏那个字标**当 exe 图标。

    以前这里钉的是「上紫下蓝的方形渐变」—— 那是另一张图。现在图标内容
    必须是横条字标：宽明显大于高（>1.8 倍），主色偏蓝，且四周透明。
    """
    from PIL import Image
    ico = SRC / "resources" / "icon.ico"
    assert ico.is_file()
    im = Image.open(ico)
    sizes = {f[0] for f in im.info.get("sizes", ())} or {s[0] for s in im.ico.sizes()}
    assert {256, 128, 64, 48, 32, 16} <= sizes, f"图标缺尺寸：{sizes}"
    im = Image.open(ico)
    im.size = (256, 256)
    rgba = im.convert("RGBA")
    box = rgba.getbbox()
    assert box, "图标整张透明"
    cw, ch = box[2] - box[0], box[3] - box[1]
    assert cw > ch * 1.8, f"图标内容不是横条字标（{cw}x{ch}）"
    # 只看真正不透明的像素（半透明的星点会把均值拉暗）
    px = rgba.load()
    tot, n = [0, 0, 0], 0
    for y in range(box[1], box[3]):
        for x in range(box[0], box[2]):
            r, g, b, a = px[x, y]
            if a > 200:
                tot[0] += r
                tot[1] += g
                tot[2] += b
                n += 1
    assert n > 500, "图标几乎全透明"
    mean = [t / n for t in tot]
    assert mean[2] > mean[0] + 30 and mean[2] > 200, \
        f"字标主色应是蓝色系（{['%.0f' % v for v in mean]}）"
    # 画布必须比内容大：字标居中留白，不是被拉伸铺满
    assert ch < 240 and cw < 250, "内容被拉伸铺满画布了"


def test_icon_source_png_is_kept():
    """字标 PNG 原图必须还在（图标就是由它生成的，删了就无法复现）。"""
    p = SRC / "resources" / "logo_wordmark.png"
    assert p.is_file(), "侧栏字标 PNG 不见了"
    from PIL import Image
    im = Image.open(p)
    assert im.width > im.height * 1.8, "这张 PNG 不是横条字标"


# ─────────────── ② 窗口：用回系统标题栏（V0.0.39 改判） ───────────────
def test_system_titlebar_restored():
    """V0.0.36 曾改成自绘标题栏，V0.0.39 用户看过实物后要求改回系统窗口。

    标题栏回到系统那套以后，✕ 的处理改过两次（见下一条）：V0.0.36/39 是
    「关窗即退出」，V0.0.45 起是「关窗收进托盘」。这里只钉「用系统标题栏」，
    关窗语义交给 test_window_close_hides_to_tray。
    """
    s = _read("media_resource_manager/gui/web_shell.py")
    assert "FramelessWindowHint" not in s, "又退回自绘无边框窗口了"
    assert "class _TitleBar" not in s, "自绘标题栏还在"
    seg = s.split("class _Win(QMainWindow)", 1)[1].split("class _ShellInputFilter", 1)[0]
    assert "closeEvent" in seg, "窗口关闭没接管"
    assert "_mrm_quitting" in seg


def test_tray_still_offers_hide_to_tray():
    """窗口上没有「缩小到托盘」按钮，这条路必须还在托盘菜单里。"""
    s = _read("media_resource_manager/gui/web_shell.py")
    assert "_hide_win" in s or "_hide_to_tray" in s
    tray = _read("media_resource_manager/gui/tray.py")
    assert "隐藏到托盘" in tray, "托盘菜单里没有「隐藏到托盘」"


def test_window_close_hides_to_tray():
    """V0.0.45 用户定稿：✕ = 收进托盘（采集继续），彻底退出走网页右下角/托盘菜单。

    原话：「还有右上角的关闭，现在变成彻底关闭了。没有最小化的托盘。」
    这条推翻 V0.0.37 的「关窗即退出」—— 那次是为了自绘标题栏里没有托盘按钮，
    现在系统 ✕ 必须回到「最小化到托盘」这个 Windows 常规行为。
    """
    s = _read("media_resource_manager/gui/web_shell.py")
    seg = s.split("class _Win(QMainWindow)", 1)[1].split("class _ShellInputFilter", 1)[0]
    assert "_mrm_hide_to_tray" in seg, "关窗没收进托盘"
    assert "event.ignore()" in seg, "closeEvent 没有拦下这次关闭"
    # 只有托盘真的建起来才敢藏；否则藏了就再也叫不回来
    assert "_mrm_hide_to_tray = _hide_win if tray is not None else None" in s
    # 真退出那条链还在（托盘「退出」/ 网页「退出软件」都要用）
    assert "_mrm_on_close" in s.split("def shell_main", 1)[0], "退出回调没接进窗口"
    assert "_mrm_quitting" in s, "真退出标记丢了"


def test_statusbar_quit_is_visible_not_icon_only():
    """V0.0.45：「右下角的退出太不明显了」。常态必须带文字，不能只有一个灰图标。"""
    app = _web("App.vue")
    seg = app.split('<div class="sb-right">', 1)[1].split("</footer>", 1)[0]
    assert "quit-btn" in seg, "退出按钮还是原来那个藏起来的样式"
    assert "退出软件" in seg, "按钮常态没有文字（只有一个图标）"
    # 深色皮肤下要换亮一档的红，否则暗底深红几乎看不见
    assert 'html[data-theme="night"] .quit-btn' in app


# ───────────────────── ③ 限速时段表 ─────────────────────
def test_rate_plan_dropdown_supports_24h():
    vue = _web("views/Settings.vue")
    seg = vue.split("<!-- 2026-10-27（编号 18/19）", 1)[1].split("</section>", 1)[0]
    assert 'type="time"' not in seg, "time 控件装不下 24:00，时段表又会显示成空"
    assert "planTimes" in seg and "24:00（午夜）" in seg
    # 非整点历史数据要在下拉里可见（补一个当前值选项）
    assert "planTimes.includes(row.end)" in seg


def test_rate_plan_switch_lives_next_to_table():
    vue = _web("views/Settings.vue")
    seg = vue.split("<!-- 2026-10-27（编号 18/19）", 1)[1].split("</section>", 1)[0]
    assert "rate_plan_enabled" in seg, "启用开关不在时段表旁边"
    assert "planToggleEnabled" in vue, "开关没有自己的保存入口"
    assert "不限速" in seg and "没有打开" in vue, "没写「配了表没开开关」的提醒"


def test_bundle_carries_v037():
    d = SRC / "web" / "static" / "app" / "assets"
    js = "".join(p.read_text(encoding="utf-8", errors="replace")
                 for p in d.glob("*.js"))
    css = "".join(p.read_text(encoding="utf-8", errors="replace")
                  for p in d.glob("*.css"))
    assert "启用分时段限速" in js and "24:00（午夜）" in js, "前端忘了重新构建"
    assert "warn-note" in css


def test_version_bumped():
    import re as _re
    from media_resource_manager import VERSION
    m = _re.fullmatch(r"0\.0\.(\d+)", VERSION)
    assert m and int(m.group(1)) >= 37, f"版本回退：{VERSION}"
    spec = _read("build/pyinstaller.spec")
    assert f"V{VERSION}" in spec, "打包注释版本与 __init__.py 不一致"
