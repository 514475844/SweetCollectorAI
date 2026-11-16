"""V0.0.12 护栏：网页审核补撤销 / 随机抽检 / 智能抽样（桌面已有三能力）。

钉四件事：
  1. 路由齐全（webui 按字面量判路由的历史护栏口径，逐字面量列出）；
  2. 动作接口每条生效路径都登记**动作前**快照（漏一处 = 那类操作撤不了，
     R158 四态机每个分支都得罩住）；
  3. 撤销复用 ReviewQueue._undo_one（两端一份回滚逻辑，不许网页另写）；
  4. 栈常驻在 server 上（挂 handler = 每请求新建 = 永远撤不了，
     就是快速添加那个 self.queue AttributeError 的同族坑）。
"""
import re
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_WEBUI = (_ROOT / "media_resource_manager/webui.py").read_text(encoding="utf-8")
_QUEUE = (_ROOT / "media_resource_manager/review/queue.py").read_text(
    encoding="utf-8")
_VUE = (_ROOT / "media_resource_manager/webapp/src/views/Review.vue").read_text(
    encoding="utf-8")
_API = (_ROOT / "media_resource_manager/webapp/src/api.js").read_text(
    encoding="utf-8")


def test_routes_listed_verbatim():
    for lit in ('path == "/api/review/undo"',
                'path == "/api/review/undo-info"',
                'path == "/api/review/sample"',
                'path == "/api/review/smart"'):
        assert lit in _WEBUI, f"路由字面量缺失：{lit}"


def test_stack_lives_on_server_not_handler():
    # handler 每请求新建；撤销栈挂它上面等于永远撤不了
    assert '_WEB_RQ_ATTR = "_web_review_queue_obj"' in _WEBUI
    seg = _WEBUI[_WEBUI.index("def _web_review_queue"):]
    seg = seg[:seg.index("def _api_review_undo")]
    assert "getattr(srv" in seg and "setattr(srv" in seg, \
        "队列对象必须挂 server 常驻，不许每次请求新建"


def test_queue_methods_shared():
    # 网页不许另写一份回滚/取样：必须转调桌面同款方法
    for m in ("snapshot_web", "finalize_web", "push_web", "undo_info"):
        assert f"def {m}" in _QUEUE, f"ReviewQueue 缺 {m}"
    seg = _WEBUI[_WEBUI.index("def _api_review_undo"):]
    seg = seg[:seg.index("def _api_review_sample")]
    assert "rq.undo()" in seg, "网页撤销必须复用 ReviewQueue.undo（桌面 Ctrl+Z 同函数）"
    seg2 = _WEBUI[_WEBUI.index("def _api_review_sample"):]
    assert "sample_quarantine" in seg2 and "smart_sample" in seg2


def test_action_snapshots_all_branches():
    # 四个 done += 1 生效点后面必须紧跟快照登记（漏一支 = 该类操作不可撤）
    seg = _WEBUI[_WEBUI.index("def _api_image_action"):]
    seg = seg[:seg.index("def _web_review_queue")]
    assert seg.count("done += 1") == 4
    assert seg.count("if snap is not None: snaps.append(snap)") == 4, \
        "某个动作分支没登记撤销快照——四态机每个分支都要罩住"
    assert "snap = _rq.snapshot_web(iid)" in seg, "快照没在动作前取"
    # 压栈放在学习完成之后（finalize 补学后权重，撤销才连学习一起退）
    assert "finalize_web" in seg and "push_web" in seg


def test_vue_buttons_wired():
    for frag in ("doUndo", "doSample('sample')", "doSample('smart')",
                 "撤销上一步", "随机抽检", "智能抽样"):
        assert frag in _VUE, f"Review.vue 缺 {frag}"
    for fn in ("reviewUndo", "reviewUndoInfo", "reviewSample", "reviewSmart"):
        assert fn in _API, f"api.js 缺封装 {fn}"


def test_empty_stack_honest():
    """没有可撤操作时如实报错，不许假装撤成（桌面同款诚实口径）。"""
    tmp = pytest.importorskip("tempfile").mkdtemp()
    from pathlib import Path as P
    import os
    os.environ["MRM_HOME"] = tmp
    from media_resource_manager.db import Database
    from media_resource_manager.config import AppConfig
    from media_resource_manager.webui import _Handler
    cfg = AppConfig(db_path=P(tmp)/'m.db', library_dir=P(tmp)/'lib',
                    rules_dir=P(tmp)/'r', isolate_dir=P(tmp)/'q',
                    log_dir=P(tmp)/'l')
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    srv = types.SimpleNamespace(cfg=cfg, db=db)
    h = _Handler.__new__(_Handler)
    h.server = srv
    r = h._api_review_undo()
    assert r["ok"] is False and "没有可撤销" in r["error"]
    info = h._api_review_undo()  # 再撤一次仍如实
    assert info["ok"] is False
    db.close()
