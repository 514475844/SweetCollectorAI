"""V0.0.27 护栏：用户实测反馈的第 5 轮四条。

用户原话：
  1.「删除任务的时候能不能给个对勾选项，同时删除图片」
  2.「设置外观与启动里面居然有两个外观选择，再加上左下角，这就是 3 个外观
     选择。这对吗？」
  3.「窗口左上角的 logo 呢？怎么成了那么难看的一个图标？还有首页上方那个
     logo 也太小了吧，啥也看不到」
  4.「启动后最小化到托盘功能呢？」

这份文件分两段：
  · 行为段 —— 真建一个临时库，跑删除任务的两条路径（保留图片 / 连带删除），
    钉住「导入的本地原图只清登记、不动磁盘」这条红线（F 盘教训）；
  · 静态段 —— 钉住界面上那三处「只该有一个/必须存在」的东西，
    防止下次改动又把重复入口或托盘接回去。
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "media_resource_manager" / "webapp" / "src"


def _read(rel: str) -> str:
    return (SRC / rel).read_text(encoding="utf-8")


# ══════════════════════════════════════════════════════ 行为：删除任务 + 图片
def _handler(db, cfg):
    import types

    from media_resource_manager import webui

    class _H:
        def __init__(self):
            self.db = db
            self.cfg = cfg
            self.server = SimpleNamespace(queue=None)
            # 被调用的辅助方法直接借真 handler 的实现（不复制一份逻辑到测试里）
            for name in ("_task_images_of", "_purge_task_images"):
                setattr(self, name,
                        types.MethodType(getattr(webui._Handler, name), self))

        def _actor(self):
            return "client"

    return _H()


def _mk_task(db, name="t1"):
    db.execute("INSERT INTO tasks (name, status, entry_urls) VALUES (?, 'idle', '[]')",
               (name,))
    return int(db.query("SELECT id FROM tasks ORDER BY id DESC LIMIT 1")[0]["id"])


def _mk_image(db, task_id, path):
    db.execute(
        "INSERT INTO images (task_id, source_url, path, width, height, bytes) "
        "VALUES (?,?,?,?,?,?)",
        (int(task_id), f"http://x/{Path(path).name}", str(path), 100, 100, 10))
    return int(db.query("SELECT id FROM images ORDER BY id DESC LIMIT 1")[0]["id"])


def test_delete_task_default_keeps_images(tmp_db, tmp_config):
    """默认（不勾）仍是老语义：只删任务，图片和图库记录全留。"""
    from media_resource_manager import webui

    tid = _mk_task(tmp_db)
    f = Path(tmp_config.library_dir) / "a.jpg"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"x")
    iid = _mk_image(tmp_db, tid, f)

    r = webui._Handler._api_task_delete(_handler(tmp_db, tmp_config), tid)
    assert r["ok"] is True
    assert tmp_db.query("SELECT 1 FROM tasks WHERE id=?", (tid,)) == []
    assert tmp_db.query("SELECT 1 FROM images WHERE id=?", (iid,)), "默认竟然删了图片"
    assert f.exists(), "默认竟然动了磁盘文件"


def test_delete_task_with_images_recycles_software_files(tmp_db, tmp_config):
    """勾上「同时删除图片」：登记记录删掉，软件自己目录里的文件进回收站。"""
    from media_resource_manager import webui

    tmp_config.use_recycle = True
    tid = _mk_task(tmp_db)
    f = Path(tmp_config.library_dir) / "b.jpg"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"x")
    _mk_image(tmp_db, tid, f)

    r = webui._Handler._api_task_delete(_handler(tmp_db, tmp_config), tid,
                                        with_images=True)
    assert r["ok"] is True and r["images_deleted"] == 1
    assert tmp_db.query("SELECT 1 FROM images WHERE task_id=?", (tid,)) == []
    assert not f.exists(), "勾了删除却没清掉文件"
    trash = Path(tmp_config.isolate_dir) / "_purged" / "b.jpg"
    assert trash.exists(), "开着回收站却把文件真删了（应进 _purged）"


def test_delete_task_with_images_never_touches_imported_originals(tmp_db, tmp_config):
    """★ 红线：导入的本地原图（F 盘那类）只清登记，磁盘文件一个都不许动。"""
    from media_resource_manager import webui

    ext = Path(tmp_config.db_path).parent / "external_lib"
    ext.mkdir(parents=True, exist_ok=True)
    tmp_config.external_library_roots = [str(ext)]
    origin = ext / "keep.jpg"
    origin.write_bytes(b"x")

    tid = _mk_task(tmp_db)
    _mk_image(tmp_db, tid, origin)

    r = webui._Handler._api_task_delete(_handler(tmp_db, tmp_config), tid,
                                        with_images=True)
    assert r["ok"] is True
    assert r["images_deleted"] == 1
    assert r["images_protected"] == 1, "没有把「只清登记」的原图数出来"
    assert origin.exists(), "★★★★ 本地图库原图被删了（用户红线：F 盘不许动）"
    assert tmp_db.query("SELECT 1 FROM images WHERE task_id=?", (tid,)) == []


def test_task_list_exposes_image_count(tmp_db, tmp_config):
    """/api/tasks 每行要带这个任务名下的图片张数（删除对话框要显示真数）。"""
    from media_resource_manager import webui

    tid = _mk_task(tmp_db, "带图任务")
    _mk_image(tmp_db, tid, Path(tmp_config.library_dir) / "1.jpg")
    _mk_image(tmp_db, tid, Path(tmp_config.library_dir) / "2.jpg")

    rows = webui._Handler._api_tasks(_handler(tmp_db, tmp_config))
    row = [x for x in rows if x["id"] == tid][0]
    assert row["images"] == 2, f"任务列表没带图片张数：{row}"


# ══════════════════════════════════════════════════════ 静态：界面对应改动
def test_delete_dialog_has_checkbox_and_sends_with_images():
    t = _read("views/Tasks.vue")
    assert "同时删除这个任务名下的" in t, "删除对话框里没有「同时删除图片」这一行"
    assert 'v-model="delDlg.withImages"' in t, "没有对勾输入框"
    assert "with_images" in t, "勾选结果没发给后端"
    assert "磁盘上的文件一个都不动" in t, "没讲清本地原图不会被删"
    # 原来那句写死「图片不受影响」的 confirm 必须消失
    assert "仅删除任务配置，已下载的图片不受影响" not in t
    a = _read("api.js")
    assert "with_images" in a or "extra" in a, "api.taskCtl 没传附加参数的通道"


def test_only_one_place_to_pick_a_skin():
    """用户原话「两个外观选择 + 左下角 = 3 个，这对吗？」—— 现在只许一处。"""
    s = _read("views/Settings.vue")
    assert "k: 'web_theme'" not in s, "设置页又冒出「网页皮肤」下拉（第二个入口）"
    sm = _read("components/SkinManager.vue")
    assert "内建皮肤（单击即预览）" not in sm, "皮肤管理里又摆了内建皮肤按钮（第三个入口）"
    # 左下角那个下拉要能把自定义皮肤一起列出来（否则合并后自定义皮肤没地方选）
    a = _read("App.vue")
    assert "customSkins" in a and "optgroup" in a
    assert "changeTheme" in a and "applySkin(key)" in a


def test_brand_logo_is_wordmark_not_tiny_square():
    a = _read("App.vue")
    assert "/brand/wordmark.png" in a, "侧栏/启动页没用裁掉透明边的字标"
    css = a.split(".brand img {", 1)[1].split("}", 1)[0]
    w = float(re.search(r"width:\s*(\d+)px", css).group(1))
    assert w >= 60, f"品牌字标只有 {w}px 宽（26px 那版就是用户说「啥也看不到」）"
    # 后端要真的服务这张图（老 logo.png 保持原样给水印/图标用）
    w_ui = (ROOT / "media_resource_manager" / "webui.py").read_text(encoding="utf-8")
    assert '"/brand/wordmark.png"' in w_ui
    assert "def brand_wordmark_bytes" in w_ui


def test_shell_window_uses_app_icon_and_tray():
    sh = (ROOT / "media_resource_manager" / "gui" / "web_shell.py").read_text(encoding="utf-8")
    assert "def _app_icon" in sh and "win.setWindowIcon" in sh, "窗口没用软件自己的图标"
    assert "app.setWindowIcon" in sh, "应用级图标没设（弹窗/任务栏会是默认图标）"
    assert "build_tray" in sh, "壳窗口没接托盘"
    assert "QSystemTrayIcon.isSystemTrayAvailable" in sh, "没判托盘是否真可用（幽灵进程风险）"
    assert "tray_start_minimized" in sh and "start_in_tray" in sh
    assert "showMessage" in sh, "启动进托盘时没有气泡说明（用户会以为没启动）"


def test_tray_setting_is_writable_and_visible():
    w_ui = (ROOT / "media_resource_manager" / "webui.py").read_text(encoding="utf-8")
    seg = w_ui.split("_SETTINGS_WRITABLE = {", 1)[1].split("}", 1)[0]
    assert "tray_start_minimized" in seg, "设置项不可写 ⇒ 网页上勾了也存不下去"
    s = _read("views/Settings.vue")
    assert "k: 'tray_start_minimized'" in s
    assert "启动后最小化到托盘" in s


def test_autostart_launches_shell_with_tray():
    """开机自启要带界面地进托盘，不是跑无窗口的 serve（那样既无托盘也无窗口）。"""
    a = (ROOT / "media_resource_manager" / "autostart.py").read_text(encoding="utf-8")
    assert "--tray" in a
    m = (ROOT / "mrm_web_app.py").read_text(encoding="utf-8")
    assert "--tray" in m and "start_in_tray" in m


def test_every_page_has_block_gap():
    """用户原话「好多块与块之间没有进行空行处理」——每页块间距必须有兜底。

    V0.0.27 体检发现：Settings/Export/Maintenance/Adapters/Models/Report/
    Quarantine/Subs 八页的 `.page` 没有 gap，搜索框/页签/卡片/保存条全部
    贴在一起（实测相邻卡片缝=0px）。现在每页 `.page` 必须带 gap。
    """
    views = ["Adapters", "Export", "Maintenance", "Models", "Quarantine",
             "Report", "Settings", "Subs", "Tasks", "Gallery", "Learning",
             "Logs", "Proxies", "Review", "Urls"]
    for name in views:
        text = _read(f"views/{name}.vue")
        m = re.search(r"\.page\s*\{([^}]*)\}", text)
        assert m, f"{name}.vue 没有 .page 样式（块间距没有兜底）"
        assert "gap" in m.group(1), f"{name}.vue 的 .page 没有 gap（块会贴在一起）"


def test_single_instance_wakeup_so_tray_is_not_lost():
    sh = (ROOT / "media_resource_manager" / "gui" / "web_shell.py").read_text(encoding="utf-8")
    assert "def _ensure_single_instance" in sh
    assert "def _attach_single_instance_wakeup" in sh
    assert 'sock.write(b"show")' in sh
