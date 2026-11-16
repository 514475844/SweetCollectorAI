"""V0.0.26 护栏：四条实测反馈的落点。

用户原话（一次给了四条）：
  1.「AI 模型页缺少环境的话，你是不是应该给个按键一键下载环境…缺少几个就设置几个按键」
  2.「这个限速时段表你就改不好吗？…我已经提过多次了」
  3.「设置里面的维护页也太多，都挤在一块儿了吧？是不是也应该分开块儿啊」
  4.「把『导出』改为『导入与导出』，然后把『导入学习』功能放在这里」

这些都属于「反复出问题」的界面项，所以把结构钉死，防止下次重构又退回去。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "media_resource_manager" / "webapp" / "src"


def _web(rel: str) -> str:
    return (WEB / rel).read_text(encoding="utf-8")


def test_models_page_says_gpu_runtime_is_bundled():
    """V0.0.34：显卡推理组件已随软件打包，不许再让用户自己去 pypi 下载。

    老判据钉的是「缺环境时给三个下载按钮」—— 组件内置后那三条反而是
    自相矛盾的提示（界面说没装、实际已装）。
    """
    s = _web("views/Models.vue")
    # V0.0.40 重写：不再暴露后端名，也不给「自己去 pypi 下载」的入口；
    # 换成「随软件内置 / 内置可用 / 驱动要更新」三态说法。
    assert "已随软件内置" in s or "内置可用" in s
    assert "DirectX 12" in s, "显卡加速的说明没写清要什么条件"
    assert "pypi.org/project/onnxruntime-directml" not in s
    tpl = s.split("<template>", 1)[1]        # 只查界面文字，注释里说明历史不算
    assert "onnxruntime" not in tpl, "又把后端名写回界面了"
    assert "CUDA" not in tpl, "又把 CUDA 这个词写回界面了"
    # 驱动入口保留，且仍要在系统浏览器里打开
    assert "显卡驱动" in s
    assert 'target="_blank"' in s and 'rel="noopener"' in s


def test_rate_plan_is_aligned_grid():
    """限速时段表：V0.0.26 行式卡片 → V0.0.42 用户定稿改回**有表头的真表格**
    （行式卡片版每行自带标签，列永远对不齐，用户连问三轮「是不是超出边框了」）。

    这条不再钉「必须行式」，改钉当时真正要保的东西：表头与数据行共用同一套
    列宽（grid-template-columns）、窄窗口有降级，且不是原生 <table>。
    """
    s = _web("views/Settings.vue")
    assert 'class="plantable"' in s, "限速时段表容器不是 plantable"
    assert 'class="planhead"' in s and 'class="planrow"' in s, "缺表头行或数据行"
    # 表头与行必须共用一条列宽定义，否则又会各按内容挤
    seg = s.split(".planhead, .planrow {", 1)[1].split("}", 1)[0]
    assert "grid-template-columns" in seg, "表头与数据行没有共用列宽"
    assert "v-for=\"(row, i) in planRows\"" in s, "时段行不是按数据渲染"
    assert "<table class=\"plan\"" not in s, "别退回原生 table（列宽不可控）"


def test_store_tab_is_split_into_blocks():
    """「存储与维护」必须分块，不许 20 行挤在一张卡片里。"""
    s = _web("views/Settings.vue")
    for title in ("磁盘与空间", "备份与恢复", "清理与诊断", "程序与站点"):
        assert f"<h3>{title}</h3>" in s, f"存储页缺分块「{title}」"
    # 每块都得是独立卡片（同一个 v-show 条件的 section）
    assert s.count('v-show="onTab(\'store\')"') == 4, \
        "存储页分块数不对（应为 4 个独立 section）"
    assert "<h3>维护</h3>" not in s, "旧的大杂烩标题还在"


def test_export_renamed_and_owns_import():
    """侧栏与页面标题都得叫「导入与导出」，导入入口在这页。"""
    app = _web("App.vue")
    assert "label: '导入与导出'" in app, "侧栏还叫「导出」"
    assert "label: '导出'" not in app

    main = _web("main.js")
    assert "title: '导入与导出'" in main, "路由标题还叫「导出与素材交付」"

    ex = _web("views/Export.vue")
    # V0.0.32：进口/出口改成两个标签页（用户要求，和设置页同款）
    # V0.0.45：卡片标题从「导入本地文件夹」改成「导入图片」—— 同一个入口现在
    # 也收 WebDAV 地址，只写「本地文件夹」会让人以为 NAS/网盘导不了。
    assert "导入图片" in ex
    assert "k: 'imp'" in ex and "k: 'exp'" in ex, "导入/导出标签页缺失"
    assert "请切到「导入」标签页" in ex, "导出页顶部没指路到导入标签"

    gal = _web("views/Gallery.vue")
    assert "ImportFolder" not in gal, "图库页还有第二份导入组件"
