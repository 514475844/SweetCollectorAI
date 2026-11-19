# V0.0.67 护栏：修复图库右键业务菜单 + 网页端不显示「退出软件」
#
# 用户实测：①右键能选中、但菜单就是不弹；②网页端也有「退出软件」按钮。
# ①真根因（真机 leveldb 埋点确诊）：onCardContext 里构造 items 时用了裸
#   `reviewMode`（它是 defineProps 的 prop，<script setup> 不会自动解包），
#   抛 ReferenceError ⇒ showContextMenu 永远不执行 ⇒ 能选中但不弹菜单。
#   修复：`props.reviewMode`。命令式 DOM（contextMenu.js）方案保留。
#   卡片 mousedown(button=2)+contextmenu 都触发（客户端吞 contextmenu 也有兜底）。
# ②修法：退出按钮加 v-if=isNativeShell()，只在客户端显示。
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_right_click_uses_imperative_menu():
    """右键菜单走命令式 DOM 模块，不依赖 Vue Teleport/v-if 渲染。"""
    grid = _read("media_resource_manager/webapp/src/components/ImageGrid.vue")
    assert "from '../contextMenu'" in grid, "未导入命令式菜单模块"
    assert "showContextMenu(e.clientX, e.clientY, items)" in grid, \
        "onCardContext 未调用命令式菜单"
    # 旧的 v-if ctx 菜单模板必须已移除（它就是渲染不出来的元凶）
    assert 'v-if="ctx"' not in grid, "仍残留 v-if ctx 菜单模板"
    assert "ctx-menu" not in grid, "仍残留旧内联菜单样式"


def test_imperative_menu_appends_to_body_with_inline_style():
    """命令式菜单：直接 append 到 body、inline 样式、自己管关闭。"""
    cm = _read("media_resource_manager/webapp/src/contextMenu.js")
    assert "document.createElement('div')" in cm, "未用原生 DOM 建菜单"
    assert "document.body.appendChild" in cm, "菜单未挂到 body"
    assert "zIndex" in cm and "2147483647" in cm, "菜单层级不够高（会被遮住）"
    assert "getBoundingClientRect" in cm and "window.innerWidth" in cm, \
        "未做视口钳制（高 DPI 下会弹到屏幕外）"
    assert "onDocMouseDown" in cm and "onKeyDown" in cm, "缺少点击外部/Esc 关闭"
    assert "setTimeout" in cm, "未延迟挂关闭监听（会被触发菜单的当次 mousedown 秒关）"


def test_right_click_events_bound():
    """卡片 mousedown(button=2) + contextmenu 都绑定（客户端/网页端双保险）。"""
    grid = _read("media_resource_manager/webapp/src/components/ImageGrid.vue")
    assert '@mousedown="e => { if (e.button === 2) onCardContext(e, it) }"' in grid, \
        "卡片未挂右键 mousedown 兜底"
    assert '@contextmenu="onCardContext($event, it)"' in grid, \
        "卡片未挂 contextmenu（网页端右键）"
    seg = grid[grid.find("function onCardContext"):]
    seg = seg[:seg.find("\n}")]
    assert "e.stopPropagation()" in seg, "右键未阻止冒泡"


def test_no_bare_prop_in_context_menu():
    """真根因护栏：右键菜单构造里不能出现裸 prop（reviewMode 必须 props.reviewMode）。

    裸 prop 在 <script setup> 里是未定义变量，抛 ReferenceError，
    导致 showContextMenu 之前中断 —— 表现就是「能选中但不弹菜单」。
    """
    grid = _read("media_resource_manager/webapp/src/components/ImageGrid.vue")
    seg = grid[grid.find("function onCardContext"):]
    seg = seg[:seg.find("\n}")]
    assert "props.reviewMode" in seg, "右键菜单未用 props.reviewMode"
    assert "label: reviewMode" not in seg, "右键菜单出现裸 reviewMode（会抛错中断）"
    # 所有临时诊断埋点必须清除
    for k in ("__ctxdiag", "__clickdiag", "__beforeShow", "__afterShow", "_diag"):
        assert k not in grid, "残留诊断埋点: " + k
    assert "_diag" not in _read("media_resource_manager/webapp/src/main.js"), \
        "main.js 仍引入临时诊断脚本"


def test_ctrl_shift_multi_select():
    """Ctrl/Shift 多选：模板必须传 $event，onCardClick 处理修饰键。"""
    grid = _read("media_resource_manager/webapp/src/components/ImageGrid.vue")
    # 不能再用 .exact —— 它会拦掉带修饰键的点击（用户实测 Ctrl/Shift 无效的根因）
    assert '@click.exact=' not in grid, "卡片仍用 @click.exact（修饰键点击不触发）"
    assert '@click="onCardClick($event, it, i)"' in grid, "卡片未把事件对象传给 onCardClick"
    assert 'function onCardClick(e, it, i)' in grid, "onCardClick 未接收事件参数"
    seg = grid[grid.find("function onCardClick"):]
    seg = seg[:seg.find("\n}")]
    assert "e.shiftKey" in seg and "e.ctrlKey" in seg, "未处理 Shift/Ctrl 多选"
    assert "anchorIdx" in seg, "Shift 连选缺少锚点"
    assert "let anchorIdx" in grid, "未定义 Shift 锚点变量"


def test_right_click_keeps_multi_selection():
    """多选后右键不清空选中集；菜单动作作用于整组。"""
    grid = _read("media_resource_manager/webapp/src/components/ImageGrid.vue")
    seg = grid[grid.find("function onCardContext"):]
    seg = seg[:seg.find("\n  showContextMenu")]
    assert "selected.value.has(it.id)" in seg, "未判断右键图是否已在选中集"
    # 不能再无条件重置成单张
    assert "if (!inSel)" in seg, "右键未做「仅在未选中时单选」的保护"
    assert "targetIds" in seg, "菜单动作未使用选中集（多选会退化成单张）"
    assert "act('like', targetIds())" in seg and "act('delete', targetIds())" in seg, \
        "批量动作未作用于整个选中集"


def test_native_browser_menu_still_hidden():
    """内核右键策略不动，浏览器原生项（检查元素等）仍不出现。"""
    shell = _read("media_resource_manager/gui/web_shell.py")
    assert "view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)" in shell, \
        "右键策略被改动（浏览器原生菜单会冒出来）"


def test_quit_button_only_in_native_shell():
    """退出按钮只在客户端显示，网页端不显示。"""
    app = _read("media_resource_manager/webapp/src/App.vue")
    seg = app[app.find('<button v-if="isNativeShell()" class="quit-btn"'):]
    seg = seg[:seg.find("</button>")]
    assert seg and 'v-if="isNativeShell()"' in seg, \
        "退出按钮未加客户端判断（网页端会误导用户）"


def test_tagging_finished_reloads_gallery():
    """识别完成后必须重载图库列表，否则卡片标记不刷新（用户「没看到变化」）。"""
    grid = _read("media_resource_manager/webapp/src/components/ImageGrid.vue")
    assert 'function onTaggingFinished' in grid, "缺少识别完成后的重载处理"
    seg = grid[grid.find("function onTaggingFinished"):]
    seg = seg[:seg.find("\n}")]
    assert "reload()" in seg and "restoreProgress()" in seg,         "识别完成后未重载列表/恢复进度"
    # running watch 里必须调用它（且只在 true->false 触发，防双重重载）
    assert "onTaggingFinished()" in grid, "running watch 未触发重载"
    assert "old === true && v === false" in grid, "未防双重触发"


def test_imported_preference_source_labeled_correctly():
    """图库批量学的偏好（user_imported）必须单列，不能被误标成模型自动。"""
    stats = _read("media_resource_manager/classify/learning_stats.py")
    assert '"user_imported": 0' in stats, "来源统计未含 user_imported"
    assert 'src not in ("user_typed", "model_auto", "user_imported")' in stats, \
        "user_imported 会被强制归成 model_auto"
    assert "图库导入" in stats, "看板来源说明未显示图库导入"
