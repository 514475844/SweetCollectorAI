"""V0.0.10 护栏：网页「不喜欢」不分页面都先问原因（与桌面两端对齐）。

桌面端图库/审核里点「不喜欢」都会弹 ask_dislike_reason（左栏主题词只做
负向权重、右栏排除词才写 ai_blacklist）；网页以前只在审核页问，图库直接
落库——同一个动作两端学到的东西不一样，违背「桌面网页功能对齐」硬约定。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GRID = ROOT / "media_resource_manager" / "webapp" / "src" / \
    "components" / "ImageGrid.vue"
HTML = ROOT / "media_resource_manager" / "web" / "index.v2.html"
ASSETS = ROOT / "media_resource_manager" / "web" / "static" / "app" / "assets"


def _src() -> str:
    return GRID.read_text(encoding="utf-8")


def test_dislike_always_asks_reason():
    """dislike 分支不许再挂 reviewMode 条件：两边都要弹原因框。"""
    t = _src()
    m = re.search(r"if \(actName === 'dislike'[^{]*\{", t)
    assert m, "找不到 dislike 分支"
    assert "reviewMode" not in m.group(0), \
        "网页图库的「不喜欢」又开始不问原因了（两端不对齐回归）"
    seg = t[m.start():m.start() + 400]
    assert "dislikeOptions" in seg and "dislikeModal" in seg, \
        "dislike 分支必须先取候选词再弹模态"


def test_modal_still_splits_topic_and_exclusion():
    """模态语义不许混：未匹配主题词≠排除词（硬性规则，桌面同款）。

    V0.0.12 形态升级：点词即交（submitDislike）→ 两栏多选统一提交
    （dmSubmit），但两栏的**出口参数名**仍是 unmatched / exclusions，
    语义分开这一条不许变。
    """
    t = _src()
    assert "unmatched:" in t and "exclusions:" in t
    assert "dmSubmit" in t, "多选统一提交口没了"
    assert "不写原因，直接不喜欢" in t, "取消引导的出口必须保留"


def test_built_asset_in_sync_with_source():
    """源码改了、产物没重建 = 网页端实际跑的还是旧行为（假对齐）。

    比对方式：源码里 dislike 分支的最新形态关键字，必须能在 html 引用的
    那份产物 JS 里找到（dislikeOptions 调用在打包后仍逐字保留）。
    """
    names = re.findall(r'assets/(v2-[\w-]+\.js)',
                       HTML.read_text(encoding="utf-8"))
    assert names, "index.v2.html 没有引用产物 JS"
    js = (ASSETS / names[0]).read_text(encoding="utf-8", errors="ignore")
    assert "dislikeOptions" in js, "产物里没有取原因接口调用"
    # 产物里 dislike 分支不应再带 reviewMode 前置（搜压缩后的三元/与运算痕迹）
    i = js.find("dislikeOptions")
    window = js[max(0, i - 260):i]
    assert "reviewMode" not in window and '"reviewMode"' not in window, \
        "产物仍是旧版：图库 dislike 被 reviewMode 挡着，没重新构建"
