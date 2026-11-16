"""r41（⑧ 自动编写适配器）：锁定「网址 -> 启发式候选规则 -> 预览确认才落盘」闭环。

不依赖网络 / 文本模型：核心 propose_adapter 是纯函数，UI 联网抓取在测试里 monkeypatch。
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from media_resource_manager.crawler import adapter_proposer as ap  # noqa: E402
from media_resource_manager.crawler.rules import load_adapter  # noqa: E402


@pytest.fixture(autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


SAMPLE_HTML = """
<html><body>
  <a href="/album/123"><img src="https://x.com/t/cover.jpg" data-src="https://x.com/o/cover.jpg"></a>
  <a href="https://x.com/full/a.png"><img src="https://x.com/t/a.jpg"></a>
  <a href="https://x.com/full/b.webp"><img src="https://x.com/t/b.jpg"></a>
  <img src="https://x.com/t/c.jpg" data-original="https://x.com/o/c.png">
  <a href="/page/2" rel="next">下一页</a>
  <a href="/page/3">3</a>
</body></html>
"""


def test_propose_detects_img_and_pagination():
    """纯函数：含 img / 直链图片 / 翻页链接时，stats 与选择器正确。"""
    rule, stats = ap.propose_adapter("https://x.com/gallery/1", SAMPLE_HTML)
    assert stats["domain"] == "x.com"
    assert stats["img_tags"] >= 4
    assert stats["direct_image_links"] >= 2
    assert stats["pagination_detected"] is True
    assert stats["pagination_expr"]
    # 有直链时第一个选择器应抓 a[href=图片]
    assert rule.image_link_selectors[0]["expr"].startswith("a[href$=")
    # 落盘后能被 load_adapter 正常解析（check_rules_drift / checkup 不会误报损坏）
    assert rule.base_url and rule.allowed_domains == ["x.com"]


def test_propose_empty_html_uses_template():
    """没抓到页面时回退通用模板，仍给可用规则（不翻页）。"""
    rule, stats = ap.propose_adapter("https://y.com/", "")
    assert stats["domain"] == "y.com"
    assert stats["pagination_detected"] is False
    assert rule.image_link_selectors  # 至少 img 选择器
    assert rule.allowed_domains == ["y.com"]


def test_dump_yaml_roundtrip_loadable(tmp_path):
    """dump -> 写文件 -> load_adapter 回来可解析，不缺必备字段。

    ⚠ R161-C1 之后 load_adapter 出口会 sanitize 名字（白名单
    ^[A-Za-z0-9_-]{1,64}$ —— 名字会被拼进下载目录名，绝不许带路径字符）
    ⇒ 文件里 name="z.com" 不合法时整体回退到文件名 stem（"zcom"）。
    这是**预期行为**（安全闸）：宁可回退到安全名，也不把带点号/路径字符的
    名字放出去。不是 roundtrip 丢字段。
    """
    from media_resource_manager.crawler.rules import sanitize_adapter_name
    rule, _ = ap.propose_adapter("https://z.com/a", SAMPLE_HTML)
    p = tmp_path / "zcom.yaml"
    p.write_text(ap.dump_rule_yaml(rule), encoding="utf-8")
    back = load_adapter(p)
    want = sanitize_adapter_name(rule.name, sanitize_adapter_name(p.stem))
    assert back.name == want, \
        f"roundtrip 名字没按 sanitize 口径回退：{back.name!r} vs {want!r}"
    assert "." not in back.name and "/" not in back.name, back.name
    assert back.base_url == rule.base_url
    assert back.image_link_selectors


def test_propose_filename_avoid_collision(tmp_path):
    """不覆盖已有规则，自动加后缀。"""
    (tmp_path / "x.com.yaml").write_text("x", encoding="utf-8")
    f1 = ap.propose_filename(tmp_path, "x.com")
    assert f1 == "x.com.yaml" or f1 == "x.com-2.yaml"
    if f1 == "x.com.yaml":
        (tmp_path / "x.com.yaml")  # exists -> next call should differ
        f2 = ap.propose_filename(tmp_path, "x.com")
        assert f2 == "x.com-2.yaml"


def test_ui_preview_then_confirm_writes_file(tmp_path, monkeypatch):
    """UI 闭环：分析 -> 预览出现域名/选择器/翻页 -> 确定才写进 rules_dir。

    【C2 改造后】分析改成了后台 QThread，所以这里**必须等线程跑完**才能读预览
    ——`_do_analyze()` 现在立刻返回（这正是"不卡死"的定义），预览是线程跑完
    后通过 done 信号回主线程刷的。以前这里是同步的，加一行 wait 就行。
    """
    monkeypatch.setattr(ap, "fetch_url", lambda url, timeout=15.0, proxy=None: SAMPLE_HTML)
    # C3：「确定」后弹「已保存」是模态 QMessageBox，测试里必须桩掉，
    # 否则会挂在模态事件循环里（offscreen 下也一样挂死）。
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "information",
                        staticmethod(lambda *a, **k: None))
    saved = []
    dlg = ap.build_auto_gen_dialog(None, tmp_path, on_saved=lambda: saved.append(1))

    # 确定按钮初始禁用，确认前不写文件
    assert dlg._b_confirm.isEnabled() is False
    assert not list(tmp_path.glob("*.yaml"))

    dlg._url_in.setText("https://x.com/gallery/1")
    dlg._do_analyze()
    # C2：分析在后台线程 —— 等它结束（这就是"UI 没卡死"的代价：结果异步到）
    _wait_worker(dlg)

    text = dlg._preview.toPlainText()
    assert "x.com" in text
    assert "翻页方式" in text
    assert "YAML" in text
    assert dlg._b_confirm.isEnabled() is True

    dlg._do_confirm()
    yamls = list(tmp_path.glob("*.yaml"))
    assert len(yamls) == 1
    # 写出的文件可被 load_adapter 解析（纳入 check_rules_drift 不会误报）
    load_adapter(yamls[0])
    assert saved == [1]  # 落盘后回调刷新列表
    # C3：保存后对话框应已关闭（accept 过）
    assert dlg.result() == int(dlg.DialogCode.Accepted) or dlg.isVisible() is False
    dlg.close()
    dlg.deleteLater()


def _wait_worker(dlg, timeout_ms: int = 5000) -> None:
    """等对话框里那个后台分析线程跑完（最多 timeout_ms）。

    为什么不能直接 sleep：QThread 的 done 信号要靠事件循环派发回主线程，
    纯 sleep 的话信号排在队列里没机会处理，预览永远是空的。
    这里用 QTest.qWait —— 它会**处理事件**（不同于 sleep），正是我们要的。
    """
    from PySide6.QtTest import QTest
    waited = 0
    while waited < timeout_ms:
        w = dlg._state.get("worker")
        if w is None or not w.isRunning():
            break
        QTest.qWait(50)
        waited += 50
    # 再给事件循环一点时间把 queued 信号处理掉
    QTest.qWait(50)


def test_ui_invalid_url_no_crash(monkeypatch):
    """非法网址只警告不抛异常、不落盘。

    注意：do_analyze 内部对非法网址会调 QMessageBox.warning（静态方法、内部
    exec 模态阻塞）；无事件循环的测试里必须把它替换成不阻塞的桩，否则会挂死。
    """
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(
        QMessageBox, "warning",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok))
    dlg = ap.build_auto_gen_dialog(None, __import__("pathlib").Path("/tmp"))
    dlg._url_in.setText("not-a-url")
    dlg._do_analyze()  # 应弹警告并 return，不抛
    assert dlg._b_confirm.isEnabled() is False
    dlg.close()
    dlg.deleteLater()


def pytest_sessionfinish(session, exitstatus):
    """offscreen 下遗留的 QApplication / 顶层窗口会导致解释器退出挂起，
    这里在会话结束强制清理并退出，确保 --junit-xml 能被正常 flush。"""
    app = QApplication.instance()
    if app is not None:
        try:
            app.closeAllWindows()
            app.quit()
        except Exception:
            pass
