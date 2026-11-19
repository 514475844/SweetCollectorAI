# V0.0.64 护栏：识别控件常驻 + 工具栏窄窗分行 + 闪屏期间窗口不露脸
#
# 用户三连问「你确定都做完吗？AI识别按钮呢？右键菜单呢？闪屏还是同出」：
#  ① AI 识别控件：V0.0.62 我加了「识别完就整组隐藏」，导致 pending=0 时按钮
#     消失（用户原话「AI识别按钮呢？」）。改成图库页**常驻**，识别完显示
#     「全部已识别」+ 置灰按钮，不再消失。
#  ② 工具栏：窄窗下左右两组交错换行看着像冲突。改成筛选区、按钮区各成一行。
#  ③ 闪屏：主窗口在启动画面期间就 show() 了 → 「动画一出窗口也出、3 秒后闪」。
#     改成有闪屏时窗口先挪到屏幕外渲染，收尾时挪回原位淡入。
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_tag_control_always_visible_in_gallery():
    """识别控件显示条件只看「是不是图库页」，不再看 pending 是否归零。"""
    grid = _read("media_resource_manager/webapp/src/components/ImageGrid.vue")
    assert "const tagShow = computed(() => props.view === 'gallery')" in grid, \
        "识别控件必须图库页常驻（不得再依赖 pending>0 才显示）"
    # 旧的反模式：running||starting||pending>0 才显示 —— 必须已移除
    assert "tagImp.running || tagImp.starting || tagImp.pending > 0" not in grid, \
        "识别控件仍带「识别完就隐藏」的旧条件"
    # V0.0.65：按钮不再因 pending<=0（含未查询的 -1）被锁死
    assert "tagBtn.off" in grid and "off: false" in grid


def test_toolbar_narrow_window_rows_split():
    """窄窗（最小 1024）下工具栏改成上下两行，不再左右交错换行。"""
    grid = _read("media_resource_manager/webapp/src/components/ImageGrid.vue")
    # media query 段里 .toolbar 必须有 flex-direction: column
    seg = grid[grid.find("@media (max-width: 1120px)"):]
    seg = seg[:seg.find("\n}")]
    assert "flex-direction: column" in seg, \
        "窄窗工具栏未改成上下分行（筛选一行、按钮一行）"


def test_splash_defers_main_window():
    """有启动画面时主窗口先挪出屏幕渲染，收尾再挪回淡入。"""
    shell = _read("media_resource_manager/gui/web_shell.py")
    assert "defer_show=(splash is not None)" in shell, \
        "内置窗口未按「有无闪屏」决定是否延迟显示"
    assert "win.move(12000, 12000)" in shell, "闪屏期间窗口没挪出屏幕"
    assert "win._mrm_home = win.pos()" in shell, "没记录窗口原位"
    # 收尾时挪回原位
    fin = shell[shell.find("def _finish_splash"):]
    fin = fin[:fin.find("\ndef ")]
    assert "win.move(home)" in fin and "_mrm_home" in fin, \
        "收尾时没把窗口挪回原位"


# ── V0.0.65：pending 未知(-1) 不能误显示「全部已识别」/锁死按钮 ──────────
def test_pending_unknown_not_treated_as_all_done():
    grid = _read("media_resource_manager/webapp/src/components/ImageGrid.vue")
    # -1（还没查到）必须有单独的「统计中」分支，不能落进 else 显示「全部已识别」
    assert "tagImp.pending < 0" in grid, "pending=-1 未单独处理"
    assert "正在统计待识别图片" in grid, "pending 未知时应显示统计中"
    # 按钮不得因 pending<=0 被禁用（原来 off=pending<=0 把 -1 也锁死）
    assert "off: false" in grid, "启动识别按钮不应被 pending 锁死"
    assert "off: tagImp.pending <= 0" not in grid, "按钮仍带旧的禁用条件"

def test_gallery_refreshes_pending_on_mount_and_view_switch():
    grid = _read("media_resource_manager/webapp/src/components/ImageGrid.vue")
    assert "if (props.view === 'gallery') refreshTagPending()" in grid,         "图库页挂载/切换时未刷新待识别数"


# ── V0.0.66：筛选控件全挤第一行(nowrap) + 启动画面 logo+转圈 ──────────
def test_filter_controls_all_on_one_row():
    grid = _read("media_resource_manager/webapp/src/components/ImageGrid.vue")
    seg = grid[grid.find("@media (max-width: 1120px)"):]
    seg = seg[:seg.find("\n}")]
    # left 区强制不换行，方向/分辨率/缩略图才能和最新采集挤同一行
    assert ".toolbar .left" in seg and "flex-wrap: nowrap" in seg, \
        "筛选区未强制单行 —— 方向/分辨率/缩略图会被挤到第二行"
    assert "flex-direction: column" in seg, \
        "工具栏应筛选一行、批量按钮一行"


def test_splash_has_spinner_and_no_gradient_bar():
    shell = _read("media_resource_manager/gui/web_shell.py")
    fn = shell[shell.find("def _show_splash"):]
    fn = fn[:fn.find("\ndef ")]
    # 旋转圈：QTimer + drawArc + 深色描点
    assert "QTimer" in fn and "drawArc" in fn, "启动画面没有旋转加载圈"
    assert "QRectF(dot_x" in fn, "旋转圈缺少深色描点"
    # 旧的顶部渐变带必须去掉
    assert "QLinearGradient" not in fn and "品牌渐变细带" not in fn, \
        "顶部那条渐变装饰带还在（用户说『上面有个条』）"
    # logo 在上、转圈在下：logo y=46 < 圈 cy=182
    assert "46, logo" in fn and "cx, cy, r = W // 2, 182" in fn, \
        "logo 与转圈上下布局不对"
