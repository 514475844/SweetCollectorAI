"""V0.0.12 护栏：网页判退原因框补齐桌面 dislike_dialog 能力。

两端同一个动作必须学到一样的东西（用户硬约定）。桌面有、网页以前缺的
三样现在都要在：
  1. 两栏**多选** + 各自「全选」（原来点一个词就直接提交，一次只带一个理由）；
  2. 手填框 + 词表桥接预演（「金发」要让用户看见会记到 blond hair 上，
     否则学习闭环对普通用户仍是黑盒）——预演文案两端必须**同源实现**；
  3. 「模型误判 → 正确标签」纠正通道：正向学习，**绝不写黑名单**
     （R174 §4 底线：模型认错不许封死用户想要的东西）。
"""
import re
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_GRID = (_ROOT / "media_resource_manager/webapp/src/components/ImageGrid.vue"
         ).read_text(encoding="utf-8")
_WEB = (_ROOT / "media_resource_manager/webui.py").read_text(encoding="utf-8")
_BR = (_ROOT / "media_resource_manager/classify/label_bridge.py").read_text(
    encoding="utf-8")
_DD = (_ROOT / "media_resource_manager/gui/dislike_dialog.py").read_text(
    encoding="utf-8")


def test_modal_is_multi_select_with_select_all():
    assert "selU" in _GRID and "selE" in _GRID, "两栏勾选态没了（退回点词即交）"
    assert _GRID.count("dmSelectAll") >= 3, "两栏各自的全选按钮"
    assert "dmSubmit" in _GRID and "dmPure" in _GRID


def test_manual_and_correct_params_flow_through():
    # 后端接收 manual / correct，并且 correct 绝不进 ai_blacklist
    seg = _WEB[_WEB.index("def _apply_web_dislike_reasons"):]
    seg = seg[:seg.index("\n    # ---- B7")]
    assert "learn_manual" in seg, "手填词要走词表桥接学习"
    assert '"like", correct' in seg or '("like", correct)' in seg or \
        "learn_manual(\n                        \"like\", correct)" in seg.replace(
            "\r", ""), "误判纠正必须是 like 方向"
    # 手填词并入排除词写 ai_blacklist（桌面 do_exclude 口径）
    assert "excl_all" in seg and "manual" in seg


def test_correct_never_reaches_blacklist():
    """端到端：correct 词只进正向偏好，绝不进任务排除词。"""
    import tempfile, os, sys
    sys.path.insert(0, str(_ROOT))
    tmp = Path(tempfile.mkdtemp())
    os.environ["MRM_HOME"] = str(tmp)
    from media_resource_manager.db import Database
    from media_resource_manager.config import AppConfig
    from media_resource_manager.webui import _Handler
    from media_resource_manager.repo import upsert_image, save_task, get_task
    from media_resource_manager.models import ImageRecord, TaskConfig, TaskStatus
    cfg = AppConfig(db_path=tmp/'m.db', library_dir=tmp/'lib',
                    rules_dir=tmp/'r', isolate_dir=tmp/'q', log_dir=tmp/'l')
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    h = _Handler.__new__(_Handler)
    h.server = types.SimpleNamespace(cfg=cfg, db=db)
    t = TaskConfig(id=None, name="t", adapter_name="", entry_urls=["https://k/"])
    t.status = TaskStatus.IDLE
    t.ai_blacklist = ["马赛克"]
    tid = save_task(db, t)
    p = tmp/'lib'/'x.jpg'; p.write_bytes(b"x" * 10)
    iid = upsert_image(db, ImageRecord(id=None, source_url="s", task_id=tid,
                                       path=str(p), tags=["猫"],
                                       needs_review=True, in_quarantine=True))
    h._apply_web_dislike_reasons([tid], [], [], manual=[],
                                 correct=["红丝", "长袜"])
    bl = get_task(db, tid).ai_blacklist
    assert "红丝" not in bl and "长袜" not in bl
    pw = {x["label"]: x["weight"] for x in db.query(
        "SELECT label, weight FROM preferences WHERE task_id=0")}
    assert pw.get("红丝", 0) > 0, "纠正词必须作为正向信号"
    db.close()


def test_bridge_preview_single_source():
    """桥接预演文案只有一处实现（桌面转调共享），两端不会分叉。"""
    assert "def preview_lines" in _BR
    assert "from ..classify.label_bridge import LabelBridge, preview_lines" in _DD
    assert "return \"\\n\".join(preview_lines(words, br))" in _DD
    # 网页必须调共享接口拿预演
    assert 'path == "/api/label-bridge/preview"' in _WEB
    assert "labelBridgePreview" in _GRID
    assert "dmManualInput" in _GRID


def test_route_and_api_wired():
    for lit in ('path == "/api/label-bridge/preview"',):
        assert lit in _WEB
    api_js = (_ROOT / "media_resource_manager/webapp/src/api.js").read_text(
        encoding="utf-8")
    assert "label-bridge/preview" in api_js
