"""R146 Part D 护栏：C2 / C5 / C6 / C7 / C8 / C9 / C10 / C11 / D。

四层判据（沿用 Part A/B/C 的口径，问行为不问字面）：
  L1 静态层 —— 8 个新场景**都走 openModal**（不许各写一份）；内联表单退役；
     新路由的字面量在 webui 里（既有护栏按字面量判「接没接」）；
  L2 node 真执行 —— mrmSimilarPct / mrmModelState / mrmAdapterStatus /
     mrmCleanupSummary 的**行为**（不是 grep 中文）；
  L3 CSS —— Part D 新增的类在位（没样式 = 组件在页面上散架）；
  L4 后端 —— /api/models、/api/model/use、/api/similar 真出数据；
     未下载的模型不许「选用成功」；相似检索与桌面端同一套 simsearch。

⚠ 与被护对象同源：读磁盘上那份 app.js / app.css / index.html / webui.py，
   不复制副本，不靠记忆。
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

def _read_opt(p):
    """旧版网页（index.html / app.js / app.css）已于 2026-10-29 物理删除。

    这三个文件的读取改成容错：读不到返回空串 —— 让仍依赖它们的断言
    **逐条失败**（好定位、好删），而不是整个测试文件在收集阶段就崩掉。
    """
    try:
        return pathlib.Path(p).read_text(encoding="utf-8")
    except OSError:
        return ""


WEB = ROOT / "media_resource_manager" / "web"
APPJS = _read_opt(WEB / "static" / "js" / "app.js")
CSS = _read_opt(WEB / "static" / "css" / "app.css")
HTML = _read_opt(WEB / "index.html")
JS = WEB / "static" / "js" / "app.js"
STUB = ROOT / "tools" / "r77_dom_stub.js"
WEBUI = (ROOT / "media_resource_manager" / "webui.py").read_text(
    encoding="utf-8")

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(
    NODE is None, reason="本机没装 node ⇒ 跳过纯 JS 行为层（不是产品问题）")


def _strip_js_comments(s: str) -> str:
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    s = re.sub(r"(?m)//.*$", "", s)
    return s


def _js(body: str):
    assert NODE, "没有 node"
    script = (
        "require(%s).install();\n"
        "var m = require(%s);\n"
        "var out = (function () { %s })();\n"
        "process.stdout.write(JSON.stringify(out));\n"
        % (json.dumps(str(STUB)), json.dumps(str(JS)), body)
    )
    p = subprocess.run([NODE, "-e", script], capture_output=True, text=True,
                       timeout=60, encoding="utf-8", errors="replace")
    assert p.returncode == 0, (
        f"node 执行失败 rc={p.returncode}\nSTDOUT:{p.stdout}\nSTDERR:{p.stderr}")
    return json.loads(p.stdout)


def _fn_body(name: str) -> str:
    m = re.search(r"function %s\([^)]*\)\s*\{.*?\n\}" % re.escape(name),
                  APPJS, re.S)
    assert m, f"app.js 找不到 function {name}"
    return _strip_js_comments(m.group(0))


# ==================== L1 · 静态层 ====================













def test_d_backend_routes_are_literals():
    """新接口必须是**显式字面量**：既有护栏按源码字面量判「接没接路由」。
    （改成 f-string 循环展开后字面量消失 ⇒ 护栏假红。）"""
    for route in ('"/api/models"', '"/api/model/use"', '"/api/similar"'):
        assert route in WEBUI, f"webui 缺路由字面量 {route}"



def test_d_similar_endpoint_does_not_run_on_ui_thread():
    """订单点名「别在主线程跑推理」—— 判据：服务端是 ThreadingHTTPServer
    （一连接一线程），且接口注释里写明了这条约束。"""
    assert "ThreadingHTTPServer" in WEBUI
    seg = WEBUI.split("def _api_similar", 1)[1][:1400]
    assert "请求线程" in seg, "没写清推理跑在请求线程（下一个人会改回主线程）"
    assert "SIMILAR_CANDIDATE_CAP" in seg, "候选集没设上限（手机会一直转圈）"


# ==================== L4 · QtWebEngine 真链路 ====================


def _webengine_ok():
    try:
        from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: F401
    except Exception:
        return False
    return True

# ==================== L2 · node 真执行 ====================


@needs_node

@needs_node

@needs_node

@needs_node

# ==================== L4 · 后端 ====================


class _Cfg:
    ai_model_path = ""
    ai_backend = "builtin"
    models_dir = ""


def _make_self(db, cfg=None):
    """拿真 _Handler 的**方法**跑，只换掉 db / cfg（与 Part C 同一套做法）。

    ⚠ 必须继承 _Handler 而不是另造鸭子类型：另造会 AttributeError，
      那种「测试自己造了个假对象所以绿了」正是本项目反复点名的假闭环。
    """
    from media_resource_manager.webui import _Handler

    class _S(_Handler):
        def __init__(self, db, cfg):
            self._db = db         # 不调 super().__init__：不需要真 socket
            self._cfg = cfg

        @property
        def db(self):
            return self._db

        @property
        def cfg(self):
            return self._cfg

    return _S(db, cfg or _Cfg())


@pytest.fixture()
def app(tmp_path):
    from media_resource_manager.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.execute(
        "INSERT INTO tasks(name, adapter_name, status) "
        "VALUES ('t1','a1','running')")
    db.execute(
        "INSERT INTO images(id, task_id, source_url, path, tags, phash) "
        "VALUES (1,1,'http://x/1.jpg',NULL,'[\"金发\"]','abc')")
    yield _make_self(db, _Cfg()), db, tmp_path
    db.close()


def test_models_lists_desktop_catalog_with_states(app):
    """C9：清单来自桌面端同一个 models_lib（不是网页另写一份）。"""
    self_, _db, _tmp = app
    out = self_._api_models()
    assert out["ok"] is True
    assert out["items"], "模型清单是空的"
    first = out["items"][0]
    for k in ("id", "name", "size_hint", "hardware", "ready", "builtin",
              "current", "path"):
        assert k in first, f"模型项缺字段 {k}"
    # 4 档 CLIP 排前面（桌面端 CLIP_ORDER 同一口径）
    assert first["id"] == "clip-vit-b32", \
        f"4 档 CLIP 没排在最前（实得 {first['id']}）"
    assert "CPU" in first["hardware"] or "GPU" in first["hardware"]
    assert isinstance(out["usage"], str) and out["usage"]


def test_models_marks_current_by_resolved_path(app):
    """「使用中」要按**磁盘路径**判定，不是按名字猜。"""
    self_, _db, _tmp = app
    items = self_._api_models()["items"]
    target = items[0]
    self_.cfg.ai_model_path = target["path"]
    again = self_._api_models()["items"]
    assert again[0]["current"] is True, \
        "配置指向它却没标「使用中」⇒ 用户不知道自己在用哪一档"
    assert sum(1 for i in again if i["current"]) == 1, \
        "同一时刻只能有一档「使用中」"


def test_model_use_rejects_not_downloaded_model(app):
    """没下载的模型不许「选用成功」—— 选了等于把 AI 关掉还说不清原因。

    ⚠ 测试要**自己找**一个「没下载也没内置」的档，不能写死 clip-vit-b32：
       那一档是随软件内置的（bundled_ready=True），写死就会走成功分支，
       这条护栏从此变成「永远绿」的空壳。
    """
    self_, _db, _tmp = app
    items = self_._api_models()["items"]
    pick = [i for i in items if not i["ready"] and not i["builtin"]]
    assert pick, "清单里竟然没有「未下载」的档（清单或判定坏了）"
    out = self_._api_model_use({"id": pick[0]["id"]})
    assert out["ok"] is False
    assert "尚未下载" in (out.get("error") or ""), \
        f"拒绝理由要说明白：{out.get('error')}"
    assert "请先在桌面端完成下载" in out["error"]


def test_model_use_sets_path_backend_and_persists(app, monkeypatch):
    """已就绪的档：写 ai_model_path、落盘。

    ⚠ 不落盘的「选用」是假动作：用户以为换了模型，跑起来还是旧的。
    V0.0.40 改判：**不再顺手把 ai_backend 改写**。builtin 与 local 在引擎里
    走同一条路（都读 ai_model_path），界面上也合并成一个选项了；这时再改写
    只会让配置文件里留一个用户没选过的值（用户原话「逻辑上都有问题」）。
    """
    import media_resource_manager.config as cfgmod

    self_, _db, tmp = app
    base = tmp / "models"
    mid = "clip-vit-b32"
    d = base / mid
    d.mkdir(parents=True)
    (d / "model.onnx").write_bytes(b"0" * 4096)      # >1KB，is_ready 才认
    (d / "config.json").write_text("{}", encoding="utf-8")
    self_.cfg.models_dir = str(base)

    before_backend = getattr(self_.cfg, "ai_backend", "")
    saved = []
    monkeypatch.setattr(cfgmod, "save_config", lambda c: saved.append(c))
    out = self_._api_model_use({"id": mid})
    assert out["ok"] is True, out
    assert self_.cfg.ai_model_path == str(d)
    assert getattr(self_.cfg, "ai_backend", "") == before_backend, \
        "选模型时又偷偷改了 ai_backend（用户没选过这个值）"
    assert saved, "没调用 save_config（重启就丢了 = 假选用）"


def test_similar_reports_missing_image_in_chinese(app):
    self_, _db, _tmp = app
    from urllib.parse import urlparse

    out = self_._api_similar(urlparse("/x?id=9999"))
    assert out["ok"] is False
    assert "不在库里" in (out.get("error") or ""), out.get("error")


def test_similar_rejects_bad_id(app):
    self_, _db, _tmp = app
    from urllib.parse import urlparse

    out = self_._api_similar(urlparse("/x?id=abc"))
    assert out["ok"] is False and "编号" in out["error"]


def test_similar_no_candidate_is_ok_not_500(app):
    """库里没有带标签的图 ⇒ ok + 空结果（不是 500，也不是假装有结果）。"""
    self_, _db, _tmp = app
    from urllib.parse import urlparse

    out = self_._api_similar(urlparse("/x?id=1"))
    assert out["ok"] is True, out
    assert out["items"] == []
    assert out["candidates"] == 0
    assert out["seed"] == 1


def test_similar_ranks_with_real_images(app):
    """真解码两张图 ⇒ 有候选、有分数、按分数降序（不是随手排序）。"""
    PIL = pytest.importorskip("PIL.Image")
    import os

    self_, db, tmp = app
    imgdir = tmp / "imgs"
    imgdir.mkdir()
    paths = []
    # ⚠ 必须是**噪声图**：纯色 PNG 压到几百字节，会被 features_for_image
    #   的 `st_size < 2048` 判为「读不出来」⇒ 候选 0、这条护栏变空壳。
    for i in range(3):
        p = imgdir / f"{i}.png"
        img = PIL.frombytes("RGB", (96, 128), os.urandom(96 * 128 * 3))
        img.save(p)
        assert p.stat().st_size > 2048, "测试图太小，换更大的"
        paths.append(str(p))
    db.execute("DELETE FROM images")
    for i, p in enumerate(paths, start=1):
        db.execute(
            "INSERT INTO images(id, task_id, source_url, path, tags, phash) "
            "VALUES (?,1,?,?,?,?)",
            (i, f"http://x/{i}.jpg", p, '["金发"]', "ff" * 8))
    # 同一张图再挂一条**没有标签**的记录：它不该进候选池（与桌面端同口径，
    # 无标签的图在主题词那一路恒为 0，混进来只会稀释结果）。
    db.execute(
        "INSERT INTO images(id, task_id, source_url, path, tags, phash) "
        "VALUES (99,1,'http://x/99.jpg',?,?,?)",
        (paths[0], "[]", "00" * 8))

    from urllib.parse import urlparse

    out = self_._api_similar(urlparse("/x?id=1&limit=10"))
    assert out["ok"] is True, out
    assert out["candidates"] == 3, \
        f"无标签的图不该进候选池（实得 {out['candidates']}）"
    assert len(out["items"]) >= 1, "有候选却没结果"
    assert 99 not in [it["id"] for it in out["items"]], \
        "无标签的图进了结果（候选池过滤失效）"
    scores = [it["score"] for it in out["items"]]
    assert scores == sorted(scores, reverse=True), "结果没按相似度降序"
    for it in out["items"]:
        assert 0.0 <= it["score"] <= 1.0, f"分数越界：{it['score']}"
        assert it["url"].startswith("/thumb/"), "前端要靠这个 URL 取缩略图"
        assert it["id"] != 1, "种子自己不该出现在结果里"


def test_task_detail_carries_status_progress_and_errors(app):
    """C5 详情要的「状态 / 两个张数 / 最近报错」由后端给，前端不自己拼。"""
    self_, db, _tmp = app
    row = db.query("SELECT id FROM tasks")[0]
    out = self_._api_task_detail(row["id"])
    assert out["ok"] is True, out
    t = out["task"]
    assert t["status"] == "running", f"详情没带状态：{t.get('status')}"
    assert set(t["progress"]) == {"got", "total"}, "缺两个张数中的某一个"
    assert isinstance(t["errors"], list)
    assert "last_error" in t
    # 旧字段一个不能少（编辑表单还靠它们）
    for k in ("entry_urls", "ai_labels", "ai_blacklist", "adapter_name"):
        assert k in t, f"详情丢了旧字段 {k}"


def test_rule_text_is_truncated_and_safe():
    """D：规则原文要截断（几百条一起下发会拖垮首屏），读不了就空串。"""
    from media_resource_manager.webui import _rule_text, RULE_TEXT_LIMIT

    assert _rule_text(pathlib.Path("不存在的/xx.yaml")) == "", \
        "读失败必须返回空串，不能把整个适配器页打成 500"
    big = pathlib.Path(
        __file__).parent.parent / "media_resource_manager" / "webui.py"
    s = _rule_text(big)
    assert len(s) <= RULE_TEXT_LIMIT + 40, "没截断"
    assert "已省略" in s, "截断没给提示（用户会以为规则就这么短）"
