"""V0.0.42 护栏：限速时段表改成真表格（铺满卡片）+ 扫码登录入口撤掉。

用户连着两次指着同一块说「UI 不对 / 又超出边框」。真实测量：
卡片内容宽 818px，而每行只有 640px（`.planlist { max-width: 640px }`），
右边空出 177px；行内还自带一排小标题（开始/结束/上限）跟表头重复；
删除键是实心大红块，在一排浅色控件里像顶出边框的异物。

同时用户定「手机扫码和敏感图打码都不要了」——扫码入口撤掉，
后端接口保留（老客户端/脚本还能用），只是网页不再给按钮。
"""
from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "media_resource_manager"


def _read(rel: str) -> str:
    p = SRC / rel
    assert p.is_file(), f"缺文件 {rel}"
    return p.read_text(encoding="utf-8")


def _plan_seg() -> str:
    """整块 <section>（限速时段表那一张卡片），从 section 开标签到它自己的闭合。

    不能用「限速时段表 … 失败重试与限速」两个关键词切：前者在同一文件的注释里
    先出现过、后者是另一个分组的标题常量，切出来是空的。
    """
    s = _read("webapp/src/views/Settings.vue")
    # 以表格容器为锚点往前找 <section：`限速时段表` 这个字在文件前面的注释里
    # 也出现过，直接拿它当锚会锚到注释上。
    i = s.index('class="planhead"')
    start = s.rindex("<section", 0, i)
    end = s.index("</section>", i)
    seg = s[start:end]
    assert "限速时段表" in seg, "切出来的不是限速那一块"
    return seg


# ───────────────── ① 表格铺满，不再固定 640 ─────────────────
def test_plan_table_fills_the_card():
    vue = _read("webapp/src/views/Settings.vue")
    seg = _plan_seg()
    assert 'class="plantable"' in seg, "时段表没用表格容器"
    assert ".plantable" in vue and ".planhead" in vue, "缺表格/表头样式"
    # 铺满的关键：最后一列吃掉剩余宽度
    assert "minmax(" in vue and "1fr" in vue, "列宽没有弹性列，铺不满卡片"
    # 旧的固定宽度与行内小标题一律不许回来
    assert "max-width: 640px" not in vue, "又出现固定 640 宽（右边会空一大块）"
    assert 'class="planlist"' not in vue, "旧的 .planlist 容器又回来了"
    assert 'class="pf"' not in vue, "行内小标题（.pf）又回来了，跟表头重复"


def test_plan_header_appears_once():
    seg = _plan_seg()
    assert seg.count('class="planhead"') == 1, "表头应只出现在表格上方一次"
    head = seg[seg.index('class="planhead"'):seg.index("</div>", seg.index('class="planhead"'))]
    for label in ("开始", "结束", "上限（KB/s）", "说明"):
        assert label in head, f"表头缺「{label}」"


def test_plan_delete_is_low_key():
    """删除键改成红字无底色 —— 原来那块实心大红是「溢出感」的主要来源。"""
    seg = _plan_seg()
    assert 'class="btn sm danger-t"' in seg, "删除键没换成低干扰样式"
    assert 'class="btn sm danger"' not in seg, "删除键还是实心红块"
    vue = _read("webapp/src/views/Settings.vue")
    assert ".btn.danger-t" in vue and "justify-self: end" in vue, \
        "危险键样式缺失或没跟右侧列对齐"


def test_plan_table_narrow_screen_falls_back():
    vue = _read("webapp/src/views/Settings.vue")
    seg = vue[vue.index(".plantable"):]
    assert "@media" in seg, "窄屏没有降级规则"
    assert ".phint { display: none" in seg, "窄屏没有先牺牲「说明」列"


# ───────────────── ② 扫码登录入口撤掉 ─────────────────
def test_qr_login_entry_removed():
    app = _read("webapp/src/App.vue")
    for bad in ("startQr", "手机扫码登录", "qrbox", "/api/login/qrcode"):
        assert bad not in app, f"网页上还有扫码登录的残留：{bad}"
    # 样式也跟着清掉，别留下无引用的死规则
    assert ".qrops" not in app


def test_qr_backend_kept_for_old_clients():
    """入口撤掉 ≠ 接口删掉：老脚本/老客户端还在调这四个地址。"""
    w = _read("webui.py")
    for path in ('"/api/login/qrcode"', '"/api/login/qrcode/image"',
                 '"/api/login/qrcode/status"', '"/api/login/qrcode/confirm"'):
        assert path in w, f"后端接口被误删：{path}"


# ───────────────── ③ 版本 ─────────────────
def test_version_at_least_v042():
    from media_resource_manager import __version__
    parts = tuple(int(x) for x in __version__.split("."))
    assert parts >= (0, 0, 42), f"版本号没升：{__version__}"

# ───────────────── ④ 这张卡片的内边距必须跟同页其他卡片一致 ─────────────────
def test_plan_card_padding_matches_siblings():
    """V0.0.43：这一块原来少了 `sec` 类 ⇒ padding 0，标题离边框只有 1px、
    表格行贴着边框画；同页其他卡片都是 18px。实测（内置浏览器量的）：
    改前标题 inset=1px，改后 19px，与「失败重试与限速」等卡片一致。

    用户连着几版说的「超出边框」，指的就是内容顶到卡片边框。
    """
    seg = _plan_seg()
    assert 'class="card sec plancard"' in seg, "限速卡片又丢了 sec 类（内边距归零）"
    assert 'class="card" v-if="onTab(\'net\')' not in seg, "旧的裸 card 又回来了"
    vue = _read("webapp/src/views/Settings.vue")
    assert ".plancard" in vue, "缺 .plancard 的间距规整"
    # 块内子元素不能再自带外边距，否则跟 grid 缝叠加成大空隙
    seg_css = vue[vue.index(".plancard {"):vue.index(".plancard {") + 400]
    assert "margin: 0" in seg_css
