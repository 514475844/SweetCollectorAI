"""V0.0.46 护栏：语义搜索（按意思找）+ 启动画面满 3 秒 + 灯箱 Teleport +
审核每页张数可调 + .inline 全局间距。

背景（用户四条 + 一条新功能）：
  1. 图库/审核/隔离区共用的搜索框要能「输入画面特征，按内容找图」
     （对标飞牛 Seek）。CLIP 图片向量落库、查询文本编码后全库余弦排序；
     中文查询先过中英词典（与打标桥接同一本），盖不住才问在线 AI。
  2. 启动画面至少 3 秒后才拉主窗口（原来首帧 1 秒多就收，动画白做）。
  3. 灯箱开着时上方「图库」标题一直亮着 —— 遮罩被 sticky 顶栏压在下面。
     修法：全屏遮罩 Teleport 到 body。
  4. 审核预览固定 60 张/页 → 「每页 N 张」下拉（localStorage 持久化），
     档位必须 ≤ 后端 PAGE_LIMIT_MAX，否则游标错位。
  5. 适配器页「新建规则 / 导入本地规则 / 说明」贴死 —— .inline 类被
     5 个页面使用但只有个别页面 scoped 定义过，收进全局 style.css。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _web(rel: str) -> str:
    return _read("media_resource_manager/webapp/src/" + rel)


# ─────────────── ① 语义搜索：后端 ───────────────
def test_semantic_module_exists():
    s = _read("media_resource_manager/semantic.py")
    # 三条纪律都要在：MIN_SIM 下限、模型隔离（clip_model 匹配）、
    # 词典优先在线兜底
    assert "MIN_SIM" in s
    assert "clip_model=?" in s, "向量读取没按模型隔离，换档后会混排"
    assert "translate_one" in s, "中文查询没走中英词典（与打标同一本）"
    assert "adapter_ai" in s, "词典盖不住的句子没有在线翻译兜底"


def test_images_table_has_vector_columns():
    s = _read("media_resource_manager/db.py")
    assert '"clip_vec": "BLOB"' in s
    assert '"clip_model"' in s, "向量没记来源模型档位"


def test_tagging_saves_vector_for_free():
    """打标必须走 classify_with_vec（一次推理同时出标签+向量）。

    分开调 classify + encode_image 等于每张图跑两遍 ViT —— 这条钉的是
    「以后新导入的图天然带向量」这个产品承诺。
    """
    s = _read("media_resource_manager/import_folder.py")
    assert "classify_with_vec" in s, "打标没顺手存向量"
    assert "save_image_vec" in s


def test_clip_model_returns_vector():
    s = _read("media_resource_manager/classify/clip_model.py")
    assert "def classify_with_vec" in s
    assert "def encode_image_vec" in s
    assert "def model_id" in s or "model_id" in s, "向量没有来源标识"


def test_semantic_http_routes():
    s = _read("media_resource_manager/webui.py")
    for route in ("/api/semantic/status", "/api/semantic/search",
                  "/api/semantic/backfill", "/api/semantic/backfill/cancel"):
        assert f'"{route}"' in s, f"缺路由 {route}"
    # POST 分发区必须走 self._json(...)：直接 return dict 不写响应，
    # 连接会被掐（探针实测 WinError 10053 的根因）。
    seg = s.split('if path == "/api/semantic/backfill/cancel":', 1)[1][:120]
    assert "self._json" in seg, "cancel 路由没走 _json，会掐连接"


def test_semantic_search_reuses_img_item():
    """搜索结果必须复用 _img_item 拼字段，不许在 semantic.py 里重抄映射。"""
    s = _read("media_resource_manager/webui.py")
    seg = s.split("def _api_semantic_search", 1)[1].split("def _api_semantic_backfill", 1)[0]
    assert "_img_item" in seg


# ─────────────── ② 语义搜索：前端 ───────────────
def test_search_box_has_semantic_toggle():
    """V0.0.49 改判：用户要求语义搜索直接集成在搜索框，不要独立开关。

    原来钉「按意思找」按钮，现在钉「输入内容自动判断」——同一目标（搜索框
    里就能按画面特征找图），换了实现方式，护栏跟着改判而不是删除。
    """
    s = _web("components/ImageGrid.vue")
    assert "按意思找" not in s, "独立开关又回来了"
    assert "semAuto" in s, "缺输入内容自动判断"
    assert "semMode" in s
    assert "loadSemantic" in s
    assert "搜索画面特征" in s, "placeholder 没提示能搜画面特征"
    assert "sem_score" in s, "相似度角标丢了"


def test_semantic_backfill_entry():
    s = _web("components/ImageGrid.vue")
    assert "补算画面特征" in s, "缺历史图补算入口"
    assert "semanticBackfill" in s


def test_api_js_semantic_methods():
    s = _web("api.js")
    for m in ("semanticStatus", "semanticSearch", "semanticBackfill",
              "semanticBackfillCancel"):
        assert m in s, f"api.js 缺 {m}"


# ─────────────── ③ 启动画面 ≥ 3 秒 ───────────────
def test_splash_minimum_three_seconds():
    s = _read("media_resource_manager/gui/web_shell.py")
    seg = s.split("def _finish_splash", 1)[1]
    assert "_mrm_shown_at" in s, "启动画面没记显示时刻"
    assert "3.0" in seg, "没有 3 秒下限"
    # 等待必须用 QTimer 而不是 time.sleep（sleep 会冻住 Qt 事件循环，
    # 画面会僵在最后一条进度上）
    head = seg.split("def _open_embedded_window")[0]
    assert "QTimer" in head and "time.sleep" not in head


# ─────────────── ④ 灯箱 Teleport 到 body ───────────────
def test_lightbox_teleported_to_body():
    s = _web("components/ImageGrid.vue")
    # 灯箱 + 标签/重命名/不喜欢 三个弹窗都共用 .lb 遮罩，全都要挂 body
    assert s.count("<Teleport to=\"body\">") >= 4
    assert s.count("</Teleport>") == s.count("<Teleport to=\"body\">")
    seg = s.split('<!-- 灯箱', 1)[1][:400]
    assert "Teleport" in seg, "灯箱本体没被 Teleport 包住"


# ─────────────── ⑤ 每页张数可调 ───────────────
def test_page_size_user_settable():
    s = _web("components/ImageGrid.vue")
    assert "mrm_page_size" in s, "每页张数没持久化"
    assert "setPageSize" in s
    # 档位 ≤ 后端硬顶 200：前端写 480 后端回 200，游标会错位
    m = re.search(r"const PAGE_SIZES = \[([0-9, ]+)\]", s)
    assert m, "PAGE_SIZES 档位表没找到"
    sizes = [int(x) for x in m.group(1).split(",")]
    assert max(sizes) <= 200, f"档位 {max(sizes)} 超过后端 PAGE_LIMIT_MAX=200"
    assert 60 in sizes, "默认档位 60 必须还在选项里"
    # limit 用的是可调值，不是写死的 prop
    assert "limit: pageSize.value" in s


# ─────────────── ⑥ .inline 全局间距 ───────────────
def test_inline_defined_globally():
    s = _read("media_resource_manager/webapp/src/style.css")
    seg = s.split(".inline {", 1)
    assert len(seg) > 1, ".inline 没进全局 style.css"
    body = seg[1].split("}", 1)[0]
    assert "gap:" in body, "全局 .inline 没有 gap（贴死的根因）"
    assert "flex-wrap: wrap" in body


def test_adapters_buttons_have_gap():
    s = _web("views/Adapters.vue")
    assert 'class="inline"' in s
    # 依赖全局定义：本页没 scoped .inline 也不许退回无间距写法
    assert "新建规则" in s and "导入本地规则" in s
