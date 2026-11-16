"""r40：剩余需求的回归锁。

覆盖这一轮批量做的改动里**最容易退化**的几处：
1. 需求 2 网格不裁剪（crop=False 走 thumbnail 而非 ImageOps.fit）；
2. 需求 5 排序重复点击切倒序（按钮组 + 方向状态）；
3. 需求 20 全部通过/全部跳过三处都取消（网格菜单/列表菜单/底部按钮）；
4. 需求 15 drop_prefs 两个分支；
5. 需求 24 short_reason 必须给中文短句、认不出就原样返回（不许瞎猜）；
6. 需求 9 模型清单完整、就绪判据不是「目录存在」；
7. 需求 28 Web 端必须 Basic Auth + 未设密码不许起。
"""
from __future__ import annotations

import pytest


# ---------------- 需求 2：网格不裁剪 ----------------
def test_thumb_crop_switch_keeps_aspect(tmp_path):
    # QPixmap 必须在有 QApplication 的进程里用，否则段错误 ——
    # 这个文件里凡是碰 Qt 的用例都要先起 app。
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    _app = QApplication.instance() or QApplication([])
    from PIL import Image
    from media_resource_manager.gui.table_view import square_thumb_pixmap

    p = tmp_path / "tall.png"
    Image.new("RGB", (200, 600), (200, 30, 30)).save(p)
    sq = square_thumb_pixmap(p, 128, cache_dir=tmp_path, digest="d1")
    fi = square_thumb_pixmap(p, 128, cache_dir=tmp_path, digest="d1", crop=False)
    assert (sq.width(), sq.height()) == (128, 128), "裁剪版必须是正方形"
    assert fi.height() > fi.width(), "完整版必须保持原比例（竖图高>宽）"
    # 两版缓存不能共用同一个文件，否则切换后拿到旧图
    # R247：缩略图分桶存放，必须递归找（原来只 glob 一层，分桶后一个都看不到）
    names = {x.name for x in tmp_path.rglob("*.png")}
    assert any("sq" in n for n in names) and any("fit" in n for n in names)


# ---------------- 需求 5：排序倒序 ----------------
def test_gallery_sort_toggles_direction():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from media_resource_manager.gui.main_window import MainWindow

    w = MainWindow()
    w._ensure_single_instance = lambda *a, **k: True
    try:
        w.hide()
        # D8：顶部排序按钮组已撤，排序入口换成「表头点击接管取数」
        # （AI 分列＝逻辑列 4；正序/倒序由表头自己 cycling）。
        w._on_gallery_header_sort(4, "asc")
        first = w._gal_sort_desc
        assert first is False, "首次点表头应先进正序"
        w._on_gallery_header_sort(4, "desc")
        assert w._gal_sort_desc != first, "再点一次必须切方向"
        assert w._gal_sort_mode == "score"
        # 属性名不能和同名方法撞（r39b 铁律：self.log 曾压过 log() 方法）。
        # _gal_sort_mode 是字符串（排序键），_gal_sort_key 才是方法。
        assert w._gal_sort_mode == "score"
        assert callable(getattr(w, "_gal_sort_key"))
    finally:
        try:
            w.close()
        except Exception:
            pass


def test_gallery_sort_widget_is_not_combobox():
    """D8：入口改成「表头点击接管取数」，顶部那组排序按钮必须真撤掉。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "media_resource_manager"
           / "gui" / "main_window.py").read_text(encoding="utf-8")
    assert 'self.g_sort = QWidget()' not in src, "顶部排序按钮组还没撤掉"
    assert "def _on_gallery_header_sort" in src, "表头接管取数没接上"
    assert "def _toggle_gallery_sort" in src


# ---------------- 需求 20：取消全部通过/跳过 ----------------
def test_batch_all_pass_and_skip_removed():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "media_resource_manager"
           / "gui" / "review_tab.py").read_text(encoding="utf-8")
    assert 'addAction("全部通过' not in src, "「全部通过」还在"
    assert 'addAction("全部跳过' not in src, "「全部跳过」还在"
    assert 'QPushButton("全部跳过")' not in src, "底部「全部跳过」按钮还在"
    # 批量仍要留着可控的路径：全选 + 跳过
    assert "def _batch_decide" in src
    assert 'menu.addAction("全选"' in src


# ---------------- 需求 15：学习进度询问 ----------------
def test_cleanup_drop_prefs_both_branches(tmp_path):
    from media_resource_manager.config import AppConfig
    from media_resource_manager.db import Database
    from media_resource_manager import cleanup

    cfg = AppConfig()
    cfg.library_dir = tmp_path / "lib"
    cfg.review_dir = tmp_path / "rev"
    cfg.isolate_dir = tmp_path / "iso"
    counts = {}
    for drop in (False, True):
        db = Database(tmp_path / f"d{int(drop)}.db")
        for i in range(3):
            db.execute("INSERT INTO preferences (label,weight,seen,task_id) "
                       "VALUES (?,?,1,?)", (f"w{i}", 0.5, i))
        db.execute("INSERT INTO tasks (name) VALUES ('t')")
        cleanup.perform_full_cleanup(db, cfg, tmp_path, drop_prefs=drop)
        counts[drop] = (db.query("SELECT COUNT(*) c FROM preferences")[0]["c"],
                        db.query("SELECT COUNT(*) c FROM tasks")[0]["c"])
        db.close()
    assert counts[False][0] == 3, "保留时应留下全部学习记录"
    assert counts[True][0] == 0, "删学习进度时应清空"
    assert counts[False][1] == 0 and counts[True][1] == 0, "任务两种情况都要清"


# ---------------- 需求 24：失败原因短句 ----------------
def test_short_reason_gives_chinese_sentences():
    from media_resource_manager.stats import short_reason

    assert "cookie" in short_reason("需要登录 / cookie missing").lower()
    assert "无法连接" in short_reason(
        "fetch failed: https://x.com/: All connection attempts failed")
    for raw in ("HTTP 403 Forbidden", "Timed out after 30s",
                "robots.txt disallow /g", "no space left on device"):
        out = short_reason(raw)
        assert out and out != raw, f"没被翻译：{raw}"
        assert any("一" <= ch <= "鿿" for ch in out), f"不是中文短句：{out}"


def test_short_reason_falls_back_without_guessing():
    """认不出来的原样返回，不许编一个结论出来。"""
    from media_resource_manager.stats import short_reason

    weird = "AttributeError: 'Engine' object has no attribute '_x'"
    assert short_reason(weird) == weird
    assert short_reason("") == ""


# ---------------- 需求 9：模型清单 ----------------
def test_model_catalog_and_readiness(tmp_path):
    from media_resource_manager import models_lib as ml

    assert len(ml.CATALOG) >= 3, "至少要给几个可选模型"
    for m in ml.CATALOG:
        assert m["id"] and m["name"] and m["repo"].startswith("https://")
        assert m["files"], f"{m['id']} 没列文件"
        assert any(f.endswith(".onnx") for f in m["files"]), \
            f"{m['id']} 没有 onnx 权重"
    # 就绪判据：光有目录不算
    d = ml.model_dir(tmp_path, "clip-vit-b32")
    d.mkdir(parents=True)
    assert not ml.is_ready(tmp_path, "clip-vit-b32"), "空目录不能算就绪"
    (d / "model.onnx").write_bytes(b"0" * 10)      # 10 字节 = 残缺
    assert not ml.is_ready(tmp_path, "clip-vit-b32"), "1KB 以下的 onnx 不算就绪"
    (d / "model.onnx").write_bytes(b"0" * 4096)
    (d / "config.json").write_text("{}", encoding="utf-8")
    assert ml.is_ready(tmp_path, "clip-vit-b32")


# ---------------- 需求 28：Web 端 ----------------
def test_web_requires_password_and_auth():
    from media_resource_manager import webui

    # 【2026-10-19 需求反转（R157）】这条断言此前钉的是「没密码 → 自动生成一个
    # 随机密码并保存 + 回显给用户」（2026-10-07 的需求）。用户真机踩坑后明确
    # 裁决反转：自动生成是**缺陷**不是保护 —— 用户从不知情，重启后被自己要密码，
    # 从没设过也不知道是多少 ⇒ 永久锁在门外（R157 用户原话）。
    # 新契约：没密码 = **免密模式**，不生成、不保存任何密码；启动必须成功，
    # 且提示文案要说明「免密」而非「已生成密码」。
    class _Cfg:
        web_password = ""
        web_user = "admin"
        web_port = 18999
        web_bind = "127.0.0.1"
    srv = webui.WebServer(_Cfg(), None)
    ok, msg = srv.start()
    assert ok, f"没设密码时 Web 应能免密启动，却起不来：{msg}"
    # ★ R157 核心：不许偷偷生成并持久化密码（这是用户被锁死的直接成因）
    assert not srv.cfg.web_password, \
        f"空密码启动竟被写入了一个密码（{srv.cfg.web_password!r}）—— R157 复发"
    assert "免密" in msg, \
        f"启动提示应点明是免密模式，实得 {msg!r}"
    try:
        # 认证比对（密码机制本身仍然有效，只是不再被自动生成）
        import base64
        real = "pw1234"
        good2 = "Basic " + base64.b64encode(
            f"admin:{real}".encode()).decode()
        assert webui.check_auth(good2, "admin", real)
        assert not webui.check_auth(good2, "admin", real + "x")
    finally:
        try:
            srv.stop()
        except Exception:
            pass

    # 认证比对
    import base64
    good = "Basic " + base64.b64encode(b"admin:pw1234").decode()
    assert webui.check_auth(good, "admin", "pw1234")
    assert not webui.check_auth(good, "admin", "other")
    assert not webui.check_auth("", "admin", "pw1234")
    bad = "Basic " + base64.b64encode(b"admin:wrong").decode()
    assert not webui.check_auth(bad, "admin", "pw1234")


def test_web_page_template_uses_dollar_not_brace():
    """页面模板不许回到「服务端拼 HTML」的老路。

    【Phase B 更新 2026-10-07】这条断言原本是「页面模板必须用
    string.Template（$body 而非 {body}）」—— 那时候界面是一段
    `webui.PAGE` 内联字符串。Phase B（B1~B9）已经把前端拆成真资产
    （`media_resource_manager/web/` 下的 html/css/js），`PAGE` 常量
    已被删除，模板问题**从根上不存在了**：不再有服务端模板。

    所以断言改成反向的护栏：内联模板不许复活，且新架构的资产必须到位。
    复活内联字符串会同时招回三个老问题 —— PWA 注册不了、CSS 复制粘贴、
    拼 HTML 漏转义就是 XSS。
    """
    from pathlib import Path

    from media_resource_manager import webui

    # 老架构不许复活
    assert not hasattr(webui, "PAGE"), \
        "内联 PAGE 模板已废弃：前端资源应放 media_resource_manager/web/"

    # 新架构：资产必须真的在位（漏打包 = 界面白屏）
    # 2026-10-29：旧版 SPA（index.html / static/css/app.css / static/js/app.js）
    # 已物理删除，只剩 Vue 新版。护栏意图不变，换成新资产清单 +
    # **构建产物**检查（漏跑 npm build 时 index.v2.html 会指向不存在的 hash）。
    web_dir = Path(webui.web_assets_dir())
    for rel in ("index.v2.html", "manifest.webmanifest", "sw.js",
                "static/js/qr-login.js"):
        assert (web_dir / rel).is_file(), f"缺前端资产 {rel}"
    _built = list((web_dir / "static" / "app" / "assets").glob("*.js"))
    assert _built, "缺前端产物 static/app/assets/*.js（是不是忘了 npm run build）"


# ---------------- 本轮新回归：日志页合并后的生命周期 ----------------
def test_only_one_log_view_is_created():
    """全文件只能有一处 `self.log_view = QTextEdit()`。

    需求 4/5 把「日志」「统计」两页合并成 `_build_log_stats_tab` 之后，
    旧的 `_build_log_tab` 没删干净 —— 它没人调用，但只要哪天被误调，
    就会**再建一个** log_view 把新页的那个顶掉，槽仍挂在旧信号上，
    运行时就是 `QTextEdit already deleted` 刷屏（第 3 轮扫描实测 216 条）。
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "media_resource_manager"
           / "gui" / "main_window.py").read_text(encoding="utf-8")
    assert src.count("self.log_view = QTextEdit()") == 1, \
        "log_view 被创建了多次，后一个会顶掉前一个"
    assert "def _build_log_tab(" not in src, "旧 _build_log_tab 没删干净"


def test_log_append_tolerates_dead_view():
    """日志控件已销毁时，log_append 必须安静跳过而不是抛 RuntimeError。"""
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication, QTextEdit
    app = QApplication.instance() or QApplication([])
    from media_resource_manager.gui.main_window import MainWindow

    w = MainWindow()
    w._ensure_single_instance = lambda *a, **k: True
    try:
        w.hide()
        lv = QTextEdit()
        w.log_view = lv
        lv.deleteLater()
        app.processEvents()
        app.processEvents()
        w.log_append("日志控件已死也不该抛")     # 绝不能 RuntimeError
        w.log_view = None
        w.log_append("控件为 None 也不该抛")
    finally:
        try:
            w.close()
        except Exception:
            pass


def test_set_stats_text_tolerates_dead_label():
    """统计 QLabel 已销毁时，_set_stats_text 必须安静跳过。

    第 4 轮扫描最后 1 条 `QLabel already deleted` 就是它 —— 日志与统计
    合并成一页后，页内控件随整棵子树被释放，而 refresh_stats 仍被队列
    事件驱动着调。
    """
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication, QLabel
    app = QApplication.instance() or QApplication([])
    from media_resource_manager.gui.main_window import MainWindow

    w = MainWindow()
    w._ensure_single_instance = lambda *a, **k: True
    try:
        w.hide()
        lbl = QLabel()
        w.stats_text = lbl
        lbl.deleteLater()
        app.processEvents()
        app.processEvents()
        w._set_stats_text("控件已死也不该抛")
        w.stats_text = None
        w._set_stats_text("为 None 也不该抛")
    finally:
        try:
            w.close()
        except Exception:
            pass


def test_refresh_stats_uses_the_safe_setter():
    """refresh_stats 里不能再有裸的 self.stats_text.setText。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "media_resource_manager"
           / "gui" / "main_window.py").read_text(encoding="utf-8")
    assert "self.stats_text.setText(" not in src, \
        "还有裸 setText，会在控件被释放后抛 RuntimeError"
    assert "def _set_stats_text(" in src


# ---------------- 需求 1/23：水印必须真的画出来 ----------------
def test_watermark_image_is_highres():
    """水印原图必须是高分辨率的长边 ≥128，不能是 ico 里的 16x16 最小帧。

    r40 踩过：`.ico` 是多帧容器，`QPixmap(str(ico))` 只拿最小帧，
    放大到水印尺寸后糊成一团，10% 透明度下等于没画 —— 用户反馈
    「没看到右下角的水印」。这里锁住原图尺寸。
    ⚠ R185 仪器修正（2026-10-20）：R175 竖排后 `watermark_pixmap()`
    返回**顺时针旋转 90°** 并裁到内容 bbox 的竖条（真 logo 241x84 →
    84 宽 x 241 高），判据从「宽 ≥128」改为「**长边 ≥128**」——
    护栏意图（防最小帧/防糊）不变，方向随竖排更新。原始 16x16 帧
    旋转后长边也只有 16，照样被抓。
    """
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from media_resource_manager.gui.watermark import watermark_pixmap

    pm = watermark_pixmap()
    assert pm is not None, "找不到水印原图"
    assert not pm.isNull()
    assert max(pm.width(), pm.height()) >= 128, \
        f"水印原图只有 {pm.width()}x{pm.height()}px，放大必糊"


def test_watermark_follows_parent_geometry():
    """水印层必须始终只占父容器右下角一小块（不再铺满整窗）。

    2026-10-06 台式机真机 bug：前一版把水印控件铺满整窗，在 paintEvent 里
    才把 logo 画到右下角 —— 等于覆盖整窗的浮层，会干扰兄弟控件重绘（按钮/
    菜单/底部统计文字只有悬停才显示、顶部菜单淡色重影）。修法：控件几何只
    占右下角小方框（≤BOX_MAX + INSET 留白），物理上盖不到别的控件。

    这里锁住两条：① 浮层是「右下角小方框」而非铺满整窗；② 父容器改尺寸时
    方框仍跟随贴在右下角（attach 里挂了 resize 转发）。
    """
    pytest.importorskip("PySide6")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QWidget
    app = QApplication.instance() or QApplication([])
    from media_resource_manager.gui.watermark import WatermarkOverlay, BOX_MAX, INSET

    host = QWidget()
    host.resize(800, 260)
    wm = WatermarkOverlay(alpha=26, parent=host)
    assert wm.attach(host)
    host.show()
    app.processEvents()

    # ① 浮层必须只占右下角一小块，绝不能铺满整窗
    assert wm.width() < host.width(), \
        f"水印铺满整窗宽了：{wm.width()} vs {host.width()}"
    assert wm.height() < host.height(), \
        f"水印铺满整窗高了：{wm.height()} vs {host.height()}"
    # 小方框边长受 BOX_MAX 约束
    assert wm.width() <= BOX_MAX, f"水印方框宽超限：{wm.width()} > {BOX_MAX}"
    assert wm.height() <= BOX_MAX, f"水印方框高超限：{wm.height()} > {BOX_MAX}"
    # 贴**左下角**（R48/A1：作业单要求从右下角改到左下角）
    # ⚠ 贴左后 OFFSET_X 归零 —— 沿用右下角那个 -50 会让 x = INSET-50 = -38
    #   直接跑出容器（#42「水印不见了」同型坑）。偏移量仍从模块读。
    from media_resource_manager.gui.watermark import OFFSET_X, OFFSET_Y
    assert wm.x() == INSET + OFFSET_X, \
        f"水印没贴左：左缘 {wm.x()} vs {INSET + OFFSET_X}"
    assert wm.y() + wm.height() == host.height() - INSET + OFFSET_Y, \
        f"水印没贴底：下缘 {wm.y()+wm.height()} vs {host.height()-INSET+OFFSET_Y}"
    assert host.layout() is None or wm not in (host.layout().itemAt(0).widget()
                                               if host.layout().count() else [],)

    # ② 改父容器尺寸 → 方框仍跟随贴左下角（attach 里挂了 resize 转发）
    host.resize(640, 400)
    app.processEvents()
    assert wm.x() == INSET + OFFSET_X, \
        f"父容器改尺寸后水印没跟上左边：{wm.x()} vs {INSET + OFFSET_X}"
    assert wm.y() + wm.height() == host.height() - INSET + OFFSET_Y, \
        f"父容器改尺寸后水印没跟上底边：{wm.y()+wm.height()} vs {host.height()-INSET+OFFSET_Y}"
    assert wm.width() < host.width(), "改尺寸后水印仍铺满整窗"
    assert wm.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents), \
        "水印不能吃掉鼠标事件"


def test_watermark_paints_pixels_in_bottom_right():
    """右下角像素必须真的被水印改动 —— 不能只验证控件存在。"""
    pytest.importorskip("PySide6")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QWidget
    app = QApplication.instance() or QApplication([])
    from media_resource_manager.gui.watermark import WatermarkOverlay

    host = QWidget()
    host.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    host.resize(700, 300)
    wm = WatermarkOverlay(alpha=40, parent=host)
    wm.setGeometry(0, 0, 700, 300)
    host.show()
    app.processEvents()

    img = host.grab().toImage()

    def avg(x0, y0, x1, y1):
        t = n = 0
        for y in range(max(0, y0), min(img.height(), y1), 2):
            for x in range(max(0, x0), min(img.width(), x1), 2):
                c = img.pixelColor(x, y)
                t += c.red() + c.green() + c.blue()
                n += 1
        return t / max(1, n)

    # 【2026-10-07】采样区按「右下角实际绘制区」取，不写死「最右下 124px」。
    # 原因：① 水印现在向左上偏了 50px（OFFSET_X/OFFSET_Y），写死的角落窗口
    # 会正好错过整块水印 → 报「没真画出来」的假失败（实测右下角采样差=0）；
    # ② 这条测试**故意**把控件设成整窗（验证 paintEvent 只在角上画），
    #    所以不能用 wm.geometry()（那是整窗）—— 必须按 BOX_MAX+偏移 推算。
    from media_resource_manager.gui.watermark import BOX_MAX, INSET
    from media_resource_manager.gui import watermark as _wm_mod
    side = min(BOX_MAX, img.width() // 3, img.height() - 2 * INSET)
    rx = img.width() - int(side) - INSET + _wm_mod.OFFSET_X
    ry = img.height() - int(side) - INSET + _wm_mod.OFFSET_Y
    br = avg(rx + 4, ry + 4, rx + side - 4, ry + side - 4)
    tl = avg(6, 6, 130, 130)
    assert abs(br - tl) > 3, "右下角没有水印痕迹（只建了控件、没真画出来）"
