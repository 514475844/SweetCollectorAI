"""R154-F22 护栏：新手引导（执行方笔记本棒 R213）。

行为断言优先、源码判据兜底；变异自检 .workbuddy/_r213_mutate.py。

覆盖（与 R154-F22 订单逐条对齐）：
- config 旗标：全新安装置 False 并落盘 / 存量配置缺键 = 兼容 True（老用户
  不打扰）/ 存值 round-trip；
- match_adapter_for_url：专属规则先于 generic-gallery 兜底 / 后缀域名匹配
  （www.x.com vs x.com）/ 无匹配 None / 非 http URL None / 规则目录缺失 None；
- 向导三步（真建 QDialog 驱动，不进 exec）：
  ① 粘贴网址 → 自动匹配提示与下拉同步 / 非法网址拦住「下一步」；
  ② apply_settings 写 cfg（backend/model_path/api/web_password）；
  ③ finish → 回调 create+start / 默认任务名 = 域名 / 非法网址回第 1 步且
  不创建 / skip → 只标完成不建任务；
- main_window 接线：设置菜单有「新手引导」且连到 open_onboarding_wizard /
  maybe_show_onboarding 旗标门 / create_first_task 真落库 / finish_onboarding
  写旗标落盘 / launch() 源码含触发钩子。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from media_resource_manager import config as config_mod
from media_resource_manager.config import AppConfig, load_config
from media_resource_manager.db import Database
from media_resource_manager.gui.onboarding import (
    WizardDialog, list_rule_names, match_adapter_for_url)
from media_resource_manager.utils.url import domain_of

MW_PY = ROOT / "media_resource_manager" / "gui" / "main_window.py"
ONB_PY = ROOT / "media_resource_manager" / "gui" / "onboarding.py"
CONFIG_PY = ROOT / "media_resource_manager" / "config.py"


# ==================== 共用夹具 ====================

def _mk_cfg(tmp_path) -> AppConfig:
    cfg = AppConfig()
    # ⚠ R213 教训：必须显式设 rules_dir/log_dir。AppConfig() 默认
    # user_data_dir()/rules → 测试会往**源码树真实 data/rules/** 写 pic.yaml，
    # 污染 21 份规则的对账口径（test_r59 的站点数判据因此变红）。
    cfg.db_path = tmp_path / "f22.db"
    cfg.library_dir = tmp_path / "library"
    cfg.review_dir = tmp_path / "review"
    cfg.isolate_dir = tmp_path / "quarantine"
    cfg.rules_dir = tmp_path / "rules"
    cfg.log_dir = tmp_path / "logs"
    for d in (cfg.library_dir, cfg.review_dir, cfg.isolate_dir,
              cfg.rules_dir, cfg.log_dir):
        d.mkdir(parents=True, exist_ok=True)
    return cfg


def _write_rule(rules_dir: Path, stem: str, name: str,
                base_url: str, allowed: list) -> None:
    rules_dir.mkdir(parents=True, exist_ok=True)
    import yaml
    p = rules_dir / f"{stem}.yaml"
    p.write_text(
        yaml.safe_dump({"name": name, "base_url": base_url,
                        "allowed_domains": allowed,
                        "image_link_selectors": [{"kind": "css",
                                                  "expr": "img",
                                                  "attr": "src"}]},
                       allow_unicode=True, sort_keys=False),
        encoding="utf-8")


def _stub_msgboxes():
    from PySide6.QtWidgets import QDialog, QMessageBox
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    QMessageBox.warning = staticmethod(lambda *a, **k: None)
    QMessageBox.critical = staticmethod(lambda *a, **k: None)
    QDialog.exec = lambda self: QDialog.DialogCode.Accepted


def _qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


# ==================== A. config 旗标语义 ====================

def test_fresh_install_flag_false_and_persisted(monkeypatch, tmp_path):
    """全新安装（config.json 不存在）→ 旗标 False 且落盘；再读仍是 False。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MRM_HOME", str(home))
    monkeypatch.setenv("MRM_NO_MIGRATE", "1")
    cfg = load_config()
    assert cfg.onboarding_done is False
    raw = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert raw["onboarding_done"] is False
    cfg2 = load_config()
    assert cfg2.onboarding_done is False          # 存值生效，不是每次 fresh


def test_existing_config_without_key_true(monkeypatch, tmp_path):
    """存量配置没有这个键 → 兼容 True（老用户升级不弹向导）。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MRM_HOME", str(home))
    monkeypatch.setenv("MRM_NO_MIGRATE", "1")
    (home / "config.json").write_text(
        json.dumps({"skin": "violet", "web_port": 8080}), encoding="utf-8")
    cfg = load_config()
    assert cfg.onboarding_done is True


def test_existing_config_flag_roundtrip(monkeypatch, tmp_path):
    """存 False 读回 False；改 True 落盘后读回 True。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MRM_HOME", str(home))
    monkeypatch.setenv("MRM_NO_MIGRATE", "1")
    cfg = load_config()
    assert cfg.onboarding_done is False
    cfg.onboarding_done = True
    config_mod.save_config(cfg)
    cfg2 = load_config()
    assert cfg2.onboarding_done is True


# ==================== B. 适配器匹配（向导第 1 步） ====================

def test_match_dedicated_before_generic_gallery(tmp_path):
    """generic-gallery 兜底必须排最后：专属规则名字典序在后也要先命中。

    ⚠ 数据设计坑（R213 变异抓到）：generic-gallery 的 allowed_domains 必须
    真能匹配该 URL，否则排不排最后结果都一样，判据钉不住排序分支。
    """
    rules = tmp_path / "rules"
    _write_rule(rules, "generic-gallery", "generic-gallery",
                "https://gallery.example", ["zzz.example", "other.com"])
    _write_rule(rules, "zzz-pic", "zzz-pic",
                "https://zzz.example", ["zzz.example"])
    assert match_adapter_for_url(rules, "https://zzz.example/album/1") == "zzz-pic"


def test_match_suffix_domain(tmp_path):
    """多级子域要能匹配父域规则（后缀三态匹配）。

    ⚠ 数据设计坑（R213 变异抓到）：`www.example.com` 会被 domain_of 剥掉
    www. 变成全等命中，「只认全等」的变异照样绿 ⇒ 必须用剥不掉的多级子域。
    """
    rules = tmp_path / "rules"
    _write_rule(rules, "pic", "pic", "https://example.com", ["example.com"])
    assert match_adapter_for_url(rules, "https://a.b.example.com/a/b") == "pic"
    # www 形态走 domain_of 剥前缀后的全等命中，顺带钉住这条路径
    assert match_adapter_for_url(rules, "https://www.example.com/a/b") == "pic"


def test_match_no_hit_returns_none(tmp_path):
    rules = tmp_path / "rules"
    _write_rule(rules, "pic", "pic", "https://example.com", ["example.com"])
    assert match_adapter_for_url(rules, "https://other-site.test/x") is None


def test_match_non_http_url_none(tmp_path):
    rules = tmp_path / "rules"
    _write_rule(rules, "pic", "pic", "https://example.com", ["example.com"])
    assert match_adapter_for_url(rules, "ftp://example.com/x") is None
    assert match_adapter_for_url(rules, "example.com/x") is None
    assert match_adapter_for_url(rules, "") is None


def test_match_missing_rules_dir_none(tmp_path):
    assert match_adapter_for_url(tmp_path / "nope", "https://example.com/x") is None


def test_list_rule_names_sorted_unique(tmp_path):
    rules = tmp_path / "rules"
    _write_rule(rules, "b-rule", "b", "https://b.test", ["b.test"])
    _write_rule(rules, "a-rule", "a", "https://a.test", ["a.test"])
    (rules / "a-rule.yaml.bak").write_text("x", encoding="utf-8")
    assert list_rule_names(rules) == ["a-rule", "b-rule"]


# ==================== C. 向导三步（真建 QDialog 驱动） ====================

def _mk_wizard(tmp_path, created=None, started=None, finished=None):
    _qapp()
    cfg = _mk_cfg(tmp_path)
    db = Database(cfg.db_path)
    rules = cfg.rules_dir
    _write_rule(rules, "pic", "pic", "https://example.com", ["example.com"])
    dlg = WizardDialog(None, cfg, db, rules,
                       on_create_task=created,
                       on_start_task=started,
                       on_finish=finished)
    return dlg, cfg, db


def test_step1_url_auto_matches_adapter_hint(tmp_path):
    dlg, cfg, db = _mk_wizard(tmp_path)
    try:
        dlg.url_edit.setText("https://example.com/album/9")
        assert "pic" in dlg.adapter_hint.text()
        assert dlg.adapter_combo.currentData() == "pic"
    finally:
        dlg.close()
        db.close()


def test_step1_invalid_url_blocks_next(tmp_path):
    _stub_msgboxes()
    dlg, cfg, db = _mk_wizard(tmp_path)
    try:
        dlg.url_edit.setText("这不是网址")
        dlg.next_page()
        assert dlg.stack.currentIndex() == 0        # 拦住，不前进
        dlg.url_edit.setText("https://example.com/x")
        dlg.next_page()
        assert dlg.stack.currentIndex() == 1        # 合法才前进
    finally:
        dlg.close()
        db.close()


def test_step2_apply_settings_writes_cfg(tmp_path):
    _stub_msgboxes()
    dlg, cfg, db = _mk_wizard(tmp_path)
    try:
        dlg.url_edit.setText("https://example.com/x")
        dlg.backend_combo.setCurrentIndex(
            dlg.backend_combo.findData("local"))
        dlg.model_path_edit.setText("/models/clip-zh")
        dlg.web_password_edit.setText("secret123")
        changed = dlg.apply_settings(cfg)
        assert cfg.ai_backend == "local"
        assert cfg.ai_model_path == "/models/clip-zh"
        assert cfg.web_password == "secret123"
        assert changed["web_password"] == "secret123"
    finally:
        dlg.close()
        db.close()


def test_step2_api_backend_fields(tmp_path):
    _stub_msgboxes()
    dlg, cfg, db = _mk_wizard(tmp_path)
    try:
        dlg.backend_combo.setCurrentIndex(
            dlg.backend_combo.findData("api"))
        dlg.api_url_edit.setText("https://api.test/v1")
        dlg.api_key_edit.setText("k-123")
        dlg.api_model_edit.setText("m-1")
        dlg.apply_settings(cfg)
        assert cfg.ai_backend == "api"
        assert cfg.ai_api_url == "https://api.test/v1"
        assert cfg.ai_api_key == "k-123"
        assert cfg.ai_api_model == "m-1"
    finally:
        dlg.close()
        db.close()


def test_step3_finish_creates_and_starts(tmp_path):
    _stub_msgboxes()
    calls = {"created": [], "started": [], "finished": 0}
    dlg, cfg, db = _mk_wizard(
        tmp_path,
        created=lambda url, adapter, name: calls["created"].append(
            (url, adapter, name)) or 42,
        started=lambda tid: calls["started"].append(tid),
        finished=lambda: calls.__setitem__("finished",
                                           calls["finished"] + 1))
    try:
        dlg.url_edit.setText("https://example.com/album/7")
        dlg.task_name_edit.setText("我的图册")
        dlg.finish()
        assert calls["created"] == [("https://example.com/album/7",
                                     "pic", "我的图册")]
        assert calls["started"] == [42]              # 勾选了立即开始
        assert calls["finished"] == 1
        assert cfg.onboarding_done is True
        assert dlg.result() == 1                     # Accepted
    finally:
        dlg.close()
        db.close()


def test_step3_default_task_name_is_domain(tmp_path):
    _stub_msgboxes()
    created = []
    dlg, cfg, db = _mk_wizard(
        tmp_path,
        created=lambda url, adapter, name: created.append(name) or 1,
        started=lambda tid: None,
        finished=lambda: None)
    try:
        dlg.url_edit.setText("https://example.com/album/7")
        assert dlg.task_name_edit.text() == "example.com"   # 预填域名
        dlg.finish()
        assert created == ["example.com"]
    finally:
        dlg.close()
        db.close()


def test_step3_uncheck_start_does_not_start(tmp_path):
    _stub_msgboxes()
    started = []
    dlg, cfg, db = _mk_wizard(
        tmp_path,
        created=lambda url, adapter, name: 7,
        started=lambda tid: started.append(tid),
        finished=lambda: None)
    try:
        dlg.url_edit.setText("https://example.com/x")
        dlg.start_now_check.setChecked(False)
        dlg.finish()
        assert started == []
    finally:
        dlg.close()
        db.close()


def test_finish_invalid_url_returns_to_step1(tmp_path):
    _stub_msgboxes()
    created = []
    finished = []
    dlg, cfg, db = _mk_wizard(
        tmp_path,
        created=lambda url, adapter, name: created.append(url) or 1,
        started=lambda tid: None,
        finished=lambda: finished.append(1))
    try:
        dlg.finish()                            # 没填网址直接点完成
        assert dlg.stack.currentIndex() == 0
        assert created == []
        assert finished == []                   # 没完成不算引导结束
    finally:
        dlg.close()
        db.close()


def test_skip_marks_done_without_create(tmp_path):
    _stub_msgboxes()
    created = []
    finished = []
    dlg, cfg, db = _mk_wizard(
        tmp_path,
        created=lambda url, adapter, name: created.append(url) or 1,
        started=lambda tid: None,
        finished=lambda: finished.append(1))
    try:
        dlg.skip()
        assert created == []
        assert finished == [1]
        assert cfg.onboarding_done is True      # 跳过 = 不再弹
    finally:
        dlg.close()
        db.close()


# ==================== D. main_window 接线 ====================

def _boot_mw(monkeypatch, tmp_path):
    """offscreen 起一个真 MainWindow（弹框/单实例已 stub，队列不起 loop）。"""
    home = tmp_path / "mrm_home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("MRM_HOME", str(home))
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("MRM_NO_MIGRATE", "1")
    monkeypatch.setenv("MRM_NO_QUEUE_LOOP", "1")
    _stub_msgboxes()
    import media_resource_manager.gui.main_window as mw
    mw._ensure_single_instance = lambda app_: True
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    win = mw.MainWindow()
    return mw, win


def test_settings_menu_has_onboarding_action(monkeypatch, tmp_path):
    mw, win = _boot_mw(monkeypatch, tmp_path)
    try:
        m_set = None
        for a in win.menuBar().actions():
            if a.text() == "设置":
                m_set = a.menu()
                break
        assert m_set is not None, "没有「设置」菜单"
        names = [a.text() for a in m_set.actions()]
        assert any("新手引导" in n for n in names), f"设置菜单缺新手引导入口: {names}"
        act = win.act_onboarding
        # PySide6 的 isSignalConnected 不吃 SignalInstance ⇒ 接线用源码判据
        # （变异删掉 connect 行同样会红）。
        src = MW_PY.read_text(encoding="utf-8")
        assert "act_onboarding.triggered.connect(self.open_onboarding_wizard)" in src
    finally:
        win.close()


def test_maybe_show_onboarding_gate(monkeypatch, tmp_path):
    mw, win = _boot_mw(monkeypatch, tmp_path)
    try:
        called = []
        monkeypatch.setattr(win, "open_onboarding_wizard",
                            lambda: called.append(1))
        win.cfg.onboarding_done = True
        win.maybe_show_onboarding()
        assert called == []                      # 旗标 True → 不弹
        win.cfg.onboarding_done = False
        win.maybe_show_onboarding()
        assert called == [1]                     # 旗标 False → 弹
    finally:
        win.close()


def test_create_first_task_persists(monkeypatch, tmp_path):
    mw, win = _boot_mw(monkeypatch, tmp_path)
    try:
        from media_resource_manager.repo import get_task
        tid = win.create_first_task("https://example.com/a", "pic", "首个任务")
        t = get_task(win.db, tid)
        assert t is not None
        assert t.name == "首个任务"
        assert t.adapter_name == "pic"
        assert list(t.entry_urls) == ["https://example.com/a"]
    finally:
        win.close()


def test_finish_onboarding_saves_flag(monkeypatch, tmp_path):
    mw, win = _boot_mw(monkeypatch, tmp_path)
    try:
        win.cfg.onboarding_done = False
        win.finish_onboarding()
        assert win.cfg.onboarding_done is True
        raw = json.loads((win.cfg.db_path.parent / "config.json")
                         .read_text(encoding="utf-8"))
        assert raw["onboarding_done"] is True
    finally:
        win.close()


def test_launch_hooks_onboarding_source():
    """launch() 源码必须含首次运行触发钩子（防接线被删）。"""
    src = MW_PY.read_text(encoding="utf-8")
    tail = src.split("def launch():", 1)[1]
    assert "maybe_show_onboarding" in tail, "launch() 里没有新手引导触发钩子"


def test_config_fresh_branch_sets_flag_source():
    """load_config 的 fresh 分支必须显式置 False（防字段加了但没接线）。"""
    src = CONFIG_PY.read_text(encoding="utf-8")
    fresh = src.split("cfg = AppConfig()", 1)[1]
    assert "onboarding_done = False" in fresh
