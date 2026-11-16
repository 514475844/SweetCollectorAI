"""V0.0.35 四件：导入子文件夹开关、切页进度不丢、灯箱不压底栏、表单间距。

判据走「关系 + 真算」：
  · 真算 = iter_images 在临时目录上跑一遍，看递归 / 不递归各收几张；
  · 关系 = 轮询状态在组件之外、灯箱有独立舞台、样式统一收口；
  · 不钉死整句文案和构建哈希，避免下次改字就误报。
"""
import inspect
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "media_resource_manager"
WEB = SRC / "webapp" / "src"


def _read(rel: str) -> str:
    p = WEB / rel
    assert p.is_file(), f"缺文件 {rel}"
    return p.read_text(encoding="utf-8")


def _py(rel: str) -> str:
    p = SRC / rel
    assert p.is_file(), f"缺文件 {rel}"
    return p.read_text(encoding="utf-8")


# ───────────────────────── ① 是否导入子文件夹 ─────────────────────────
def test_iter_images_recursive_flag(tmp_path):
    """顶层一张、子目录一张：关掉递归只能收一张，打开收两张。"""
    from media_resource_manager.import_folder import iter_images
    (tmp_path / "top.jpg").write_bytes(b"x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nested.png").write_bytes(b"x")
    (tmp_path / "sub" / "deep").mkdir()
    (tmp_path / "sub" / "deep" / "third.webp").write_bytes(b"x")
    (tmp_path / "readme.txt").write_bytes(b"x")

    flat = iter_images(tmp_path, recursive=False)
    all_ = iter_images(tmp_path, recursive=True)
    assert [p.name for p in flat] == ["top.jpg"], "不递归却把子文件夹收进来了"
    assert len(all_) == 3, "递归漏了子文件夹里的图"
    assert [p.name for p in iter_images(tmp_path)] == [p.name for p in all_], \
        "不传参数时默认必须还是全递归（老行为不能突变）"


def test_import_folder_default_is_recursive():
    """默认开：桌面端「从文件夹学习」和所有老调用点的行为都不许变。"""
    from media_resource_manager import import_folder as imp
    for fn in (imp.import_folder, imp.iter_images):
        assert inspect.signature(fn).parameters["recursive"].default is True, \
            f"{fn.__name__} 的 recursive 默认值不是 True"
    src = _py("import_folder.py")
    assert "iter_images(folder, recursive=recursive)" in src, \
        "_run 没把开关真正传给找图函数"
    run_sig = src.split("def _run", 1)[1].split("-> dict:", 1)[0]
    assert "recursive: bool = True" in run_sig, "_run 签名没接住 recursive"
    assert "recursive=recursive," in src.split("def import_folder", 1)[1].split("def ", 1)[0], \
        "import_folder 没把开关传进 _run"


def test_webui_wires_recursive_to_job():
    src = _py("webui.py")
    seg = src.split("def _api_import_folder(self, body):", 1)[1].split(
        "def _api_import_job", 1)[0]
    assert '_as_bool(body.get("recursive"), True)' in seg, "接口没读这个开关"
    assert "recursive=recursive," in seg, "接口没把开关传进后台作业"
    assert "body.get(\"copy\")" in seg, "原有复制开关被顺手删了"


def test_frontend_has_recursive_checkbox():
    comp = _read("components/ImportFolder.vue")
    assert "imp.recursive" in comp and 'type="checkbox"' in comp
    assert "包含子文件夹" in comp
    job = _read("importJob.js")
    assert "recursive: true" in job, "默认必须是勾选（保持老行为）"
    assert "recursive: imp.recursive" in job, "请求里没带这个开关"
    assert "imp.recursive = s.recursive !== false" in job, "恢复状态时把开关丢了"


# ───────────────────────── ② 切页后进度不丢 ─────────────────────────
def test_import_state_is_module_level():
    """组件会被路由销毁；状态与定时器必须活在模块里，不能跟着 ref 一起没。"""
    job = _read("importJob.js")
    comp = _read("components/ImportFolder.vue")
    assert "export const imp = reactive({" in job
    assert "resumeImport()" in job and "setInterval(_tick, 1500)" in job
    assert "localStorage" in job and "mrm.import.job.v1" in job
    assert "clearInterval(timer)" in job
    assert "from '../importJob'" in comp, "组件没接共享状态"
    assert "onUnmounted(stopTimer)" not in comp, \
        "组件卸载又停了轮询 —— 切到图库再回来进度就断"
    assert "onMounted(poll)" in comp and "function poll()" in comp, \
        "组件挂载时没接回上次的作业"
    # 界面显示的是共享状态，不是组件自己的 ref
    assert "imp.running" in comp and "imp.pct" in comp and "imp.result" in comp


# ───────────────────────── ③ 灯箱布局与关闭 ─────────────────────────
def test_lightbox_has_own_stage():
    s = _read("components/ImageGrid.vue")
    assert '<div class="lb-stage" @click.self="closeLightbox">' in s, \
        "舞台空白点击不能关闭灯箱"
    assert '@click.self="closeLightbox"' in s.split("<!-- 灯箱", 1)[1], \
        "遮罩层本身也不许丢掉点击关闭"
    assert "@click.stop=" in s, "点图片本身会把灯箱顺手关掉"
    css = s.split("<style scoped>", 1)[1]
    assert "flex: 1 1 auto; min-height: 0" in css, \
        "舞台没有吃掉剩余高度，大图又会撑开容器"
    assert ".lb-bar {" in css and "flex: none" in css, "底栏会被大图压缩"
    assert ".lb-stage {" in css and "overflow: hidden" in css, \
        "放大后的图片会盖到底栏上"
    assert "padding: 8px 0 64px" not in css, "老的两行 grid + 64px 兜底又回来了"
    assert ".lb-fs .lb-bar { display: none; }" in css, "全屏模式底栏还占着位置"


# ───────────────────────── ④ 编辑框与按钮间距 ─────────────────────────
def test_all_text_inputs_share_one_style():
    css = _read("style.css")
    assert "input:not([type])" in css, \
        "没写 type 的输入框仍会掉回浏览器原生样式（高度 21px）"
    assert "textarea" in css.split("input:not([type])", 1)[1].split("}", 1)[0], \
        "多行框没一起收口"


def test_button_groups_are_not_cramped():
    tasks = _read("views/Tasks.vue")
    assert "gap: 8px; flex-wrap: wrap; row-gap: 6px; justify-content: flex-end" in tasks
    for rel in ("views/Export.vue", "views/Settings.vue"):
        s = _read(rel)
        assert ".tabs { display: flex; gap: 8px;" in s, f"{rel} 标签还挤在 4px"
    grid = _read("components/ImageGrid.vue")
    assert ".zoomg { display: inline-flex; gap: 6px;" in grid, "灯箱缩放按钮还挤着"
    assert "gap: 8px; align-items: center; flex-wrap: wrap" in grid, "工具条按钮还挤着"


def test_built_bundle_carries_v035():
    """源码改了但忘了重新构建，这一条会先挡下来。"""
    d = SRC / "web" / "static" / "app" / "assets"
    js = "".join(p.read_text(encoding="utf-8", errors="replace")
                 for p in d.glob("*.js"))
    css = "".join(p.read_text(encoding="utf-8", errors="replace")
                  for p in d.glob("*.css"))
    for k in ("包含子文件夹", "mrm.import.job.v1", "lb-stage",
              "/api/import/folder", "导入图片"):
        assert k in js, f"前端产物里没有「{k}」，忘了 npm run build"
    for k in ("input:not([type])", "min-height:0", "lb-stage"):
        assert k in css, f"样式产物里没有「{k}」，忘了 npm run build"


def test_version_bumped():
    """钉关系不钉旧值：每次升版都改断言就又回到「钉死版本」的老毛病。"""
    import re
    from media_resource_manager import __version__
    m = re.fullmatch(r"0\.0\.(\d+)", __version__)
    assert m and int(m.group(1)) >= 35, f"版本号回退或格式不对：{__version__}"
