"""R172 护栏：AI 模型设置三项（执行方笔记本棒，2026-10-20）。

订单要点：
  #1 CPU/GPU 开关搬进 AI 模型设置对话框顶部 + 与菜单互相同步 + 网页端也补
     （读/写同一份 cfg.ai_gpu），显示**当前实际生效**的后端；
  #2 模型面板重排为卡片行（名称+一句话优点 / 状态徽章 / 主按钮，门槛收进
     「详情」折叠，当前行高亮，行内下载进度），网页端同口径；
  #3 「设为当前」即时反馈（行高亮 + 按钮变「✓ 当前」禁用 + 提示）。

判据来源：R180/R63-D3 教训 —— 只 grep 字符串的护栏会被大改照样骗过。
所以本文件：
  · 桌面端断言**真控件状态**（QCheckBox 真 toggled 后 cfg 变没变、菜单勾选
    同没同步、下拉与复选框会不会来回打架）；
  · 前端用 node **真跑** mrmModelCardHtml，看四种态渲染出来的按钮/徽章；
  · 后端接口用**真调** _api_ai_gpu / _api_models，看落盘与返回字段。

⚠ 本文件只碰 R172 相关的行，不动 data/ 用户数据、不碰 R48 的 14 红基线。
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

WEBUI = ROOT / "media_resource_manager" / "webui.py"
def _read_opt(p):
    """旧版网页（index.html / app.js / app.css）已于 2026-10-29 物理删除。

    读取改成容错：读不到返回空串 —— 让仍依赖旧 DOM/JS 的断言**逐条失败**
    （好定位、好删），而不是整个测试文件在收集阶段就崩掉。
    """
    try:
        return Path(p).read_text(encoding="utf-8")
    except OSError:
        return ""


APPJS = ROOT / "media_resource_manager" / "web" / "static" / "js" / "app.js"
APPCSS = ROOT / "media_resource_manager" / "web" / "static" / "css" / "app.css"
MAIN = ROOT / "media_resource_manager" / "gui" / "main_window.py"
THEME = ROOT / "media_resource_manager" / "gui" / "theme.py"
MODELS = ROOT / "media_resource_manager" / "models_lib.py"

_WEBUI_TXT = WEBUI.read_text(encoding="utf-8")
_APPJS_TXT = _read_opt(APPJS)
_APPCSS_TXT = _read_opt(APPCSS)
_MAIN_TXT = MAIN.read_text(encoding="utf-8")
_THEME_TXT = THEME.read_text(encoding="utf-8")


# ==================== 一、共享探测函数（两端同一份判定） ====================

def test_device_report_is_shared_by_both_ends():
    """#1 的核心防线：桌面与网页**必须**用同一个判定函数。

    两端各写一套「有没有显卡后端」的判断，迟早漂移成
    「设置页说在用 GPU、日志说在用 CPU」—— 那正是用户抱怨的不一致。
    """
    from media_resource_manager import models_lib

    for fn in ("available_providers", "device_report", "gpu_switch_text"):
        assert hasattr(models_lib, fn), f"models_lib 缺 {fn}"
    # 两端都得真的引用它（不是各写一套地方口径）
    assert "gpu_switch_text" in _MAIN_TXT or "device_report" in _MAIN_TXT, \
        "桌面端没走共享探测"
    assert "device_report" in _WEBUI_TXT, "网页端没走共享探测"
    # 反面对照：两端都不许自己写「有没有显卡后端」的名单判定
    for path, txt in (("桌面", _MAIN_TXT), ("网页", _WEBUI_TXT)):
        assert "CPUExecutionProvider" not in txt.split("models_lib")[0], \
            f"{path}端在自己算后端名单（该走 models_lib）"


def test_device_report_core_semantics():
    """纯函数口径：想要 GPU / 有 GPU / 实际用谁 / 降级提醒，四件事都要对。

    ⚠ 2026-10-27：`device_report` 多了 `small_model` 参数（auto 档下小模型
    实测 CPU 快 15 倍，所以自动走 CPU）。**本条只测四字段语义**，与模型体积
    无关，故显式传 small_model=False 固定"显卡可用就该报 gpu"这一场景。

    这是「开关打开了到底生没生效」这个问题的**唯一答案来源**，
    口径必须与 clip_onnx._pick_providers 一致：非 CPU 后端有货才算能用 GPU。
    """
    from media_resource_manager.models_lib import device_report

    # ① 要 GPU，本机有 CUDA → 真用 GPU
    r = device_report(True, ["CUDAExecutionProvider", "CPUExecutionProvider"],
                     small_model=False)
    assert (r["want"], r["have"], r["degraded"]) == ("gpu", "gpu", False), r
    # V0.0.40 改判（不是放宽）：这句话会原样显示给用户，**不许出现后端英文名**
    # （CUDA / DmlExecutionProvider 之类），只说要人话的结论。
    assert "显卡" in r["text"], r
    for bad in ("CUDA", "ExecutionProvider", "DirectML", "onnxruntime"):
        assert bad not in r["text"], f"界面文案里又出现后端名 {bad}：{r['text']}"

    # ② 要 GPU，本机只有 CPU → 必须**明确说降级**（不能静默）
    r = device_report(True, ["CPUExecutionProvider"], small_model=False)
    assert (r["want"], r["have"]) == ("gpu", "cpu"), r
    assert r["degraded"] is True, "开了 GPU 却没显卡居然不报降级"
    assert "CPU" in r["text"] and ("显卡" in r["text"] or "GPU" in r["text"]), r

    # ③ 要 CPU，本机有 GPU 也照旧用 CPU（用户显式选择优先）
    r = device_report(False, ["CUDAExecutionProvider", "CPUExecutionProvider"],
                     small_model=False)
    assert (r["want"], r["have"], r["degraded"]) == ("cpu", "cpu", False), r

    # ④ 读不到后端 → 不崩，给中文说明
    r = device_report(True, [], small_model=False)
    assert r["have"] == "cpu" and r["text"], r
    assert "onnxruntime" in r["text"] or "读不到" in r["text"], r

    # ⚠ Azure 是微软云后端，不是本机显卡 —— 不能算「有 GPU」
    r = device_report(True, ["AzureExecutionProvider", "CPUExecutionProvider"],
                     small_model=False)
    assert r["have"] == "cpu", f"把 Azure 当成显卡了：{r}"


def test_gpu_switch_text_matches_report():
    """开关旁那句状态 = device_report 的 text（两处措辞不许漂移）。"""
    from media_resource_manager import models_lib as M

    for want in (True, False):
        for prov in ([], ["CPUExecutionProvider"], ["CUDAExecutionProvider"]):
            assert M.gpu_switch_text(want, prov) == \
                M.device_report(want, prov)["text"]


# ==================== 二、桌面端：对话框顶部开关 + 菜单同步 ====================

@pytest.fixture()
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


class _Cfg:
    """最小 cfg（够 ai_model_dialog 走完构造）。"""

    ai_backend = "builtin"
    ai_model_path = ""
    ai_api_url = ""
    ai_api_key = ""
    ai_api_model = ""
    ai_gpu = True
    ai_threads = 0
    learn_enabled = True
    ui_preset = "violet"
    skin = "#8b7cf8"
    dark_mode = True
    models_dir = ""


def _open_dialog(monkeypatch, tmp_path, gpu=True):
    """真建一次 AI 模型对话框，返回 (dlg, win)。

    手法与 test_r44_ai_model_panel 一致：把 QDialog.exec 短路掉，
    对话框构造完就能拿控件断言（不真进事件循环）。
    """
    from PySide6.QtWidgets import QDialog

    from media_resource_manager.gui.main_window import MainWindow

    seen = []
    monkeypatch.setattr(
        QDialog, "exec",
        lambda self, *a, **k: (seen.append(self)
                               or QDialog.DialogCode.Rejected))

    w = MainWindow()
    w._ensure_single_instance = lambda *a, **k: True
    try:
        w.hide()
    except Exception:
        pass
    # 2026-10-27：设备改成三档后，测试必须同时设 ai_device
    # （ai_device='gpu' + ai_gpu=False 会互相打架；auto+gpu=False 才是纯 CPU）。
    w.cfg.ai_device = "auto" if gpu else "cpu"
    w.cfg.ai_gpu = bool(gpu)
    w.act_ai_gpu.setChecked(bool(gpu))
    monkeypatch.setattr(w, "_models_base_dir",
                        lambda: Path(tmp_path) / "models")
    w.ai_model_dialog()
    return seen[0], w


def test_dialog_has_gpu_switch_at_top(qapp, monkeypatch, tmp_path):
    """#1：顶部第一行要有 GPU 开关 + 一句实际状态（不能只有菜单里有）。"""
    dlg, _w = _open_dialog(monkeypatch, tmp_path, gpu=True)
    chk = getattr(dlg, "_gpu_chk", None)
    assert chk is not None, "对话框里没有 GPU 开关（订单 #1 未落地）"
    assert chk.isChecked() is True, "cfg.ai_gpu=True 但开关没勾上"
    st = getattr(dlg, "_gpu_state", None)
    assert st is not None, "开关旁没有「当前实际状态」那行"
    assert st.text().strip(), "状态行是空的 —— 用户还是不知道生没生效"


def test_dialog_gpu_switch_shows_degraded(qapp, monkeypatch, tmp_path):
    """#1：开了 GPU 但本机没显卡后端 → 状态行必须**说人话**点明降级。

    实测本机 providers 只有 CPU/Azure，所以这条在 CI 上也是稳定的。
    """
    from media_resource_manager.models_lib import device_report

    dlg, _w = _open_dialog(monkeypatch, tmp_path, gpu=True)
    st = dlg._gpu_state.text()
    expect = device_report(True)["text"]
    assert st == expect, f"状态行与共享口径不一致：{st!r} != {expect!r}"


def test_dialog_gpu_switch_writes_cfg_and_syncs_menu(qapp, monkeypatch,
                                                    tmp_path):
    """#1 的**互相同步**：点开关 → cfg 变 + 落盘 + 菜单勾选态跟着变。

    只改 cfg 不刷菜单 = 「两处同一份状态」变成两处显示不同 —— 用户看到
    菜单里还是勾着的（或反过来），会以为开关是坏的。
    """
    import media_resource_manager.config as cfgmod

    saved = []
    monkeypatch.setattr(cfgmod, "save_config", lambda c: saved.append(c))
    dlg, w = _open_dialog(monkeypatch, tmp_path, gpu=True)

    dlg._gpu_chk.setChecked(False)          # 用户点掉 GPU
    assert w.cfg.ai_gpu is False, "关掉开关后 cfg 没更新"
    assert w.act_ai_gpu.isChecked() is False, \
        "菜单勾选态没跟着关（两处状态漂移）"
    assert saved, "对话框里的开关改动没落盘（关掉对话框就丢）"

    dlg._gpu_chk.setChecked(True)           # 再开回来
    assert w.cfg.ai_gpu is True and w.act_ai_gpu.isChecked() is True


def test_dialog_device_combo_and_checkbox_stay_in_sync(qapp, monkeypatch,
                                                       tmp_path):
    """#1：顶部复选框与「通用」页下拉是同一份状态，双向都不许打架。

    ⚠ 这条是真机隐患：两控件各自写 cfg，若不同步，用户勾了复选框再点
      「保存」，do_save 会按**下拉的旧值**把刚设的值覆盖回去 —— 改动静默回滚。
    """
    dlg, w = _open_dialog(monkeypatch, tmp_path, gpu=True)
    dev = dlg._device_combo

    # ① 复选框 → 下拉
    dlg._gpu_chk.setChecked(False)
    assert dev.currentData() == "cpu", "复选框关掉了，下拉还停在 GPU"
    # ② 下拉 → 复选框（含 cfg）
    dev.setCurrentIndex(dev.findData("gpu"))
    assert dlg._gpu_chk.isChecked() is True, "下拉切 GPU，顶部复选框没跟着勾"
    assert w.cfg.ai_gpu is True, "下拉切 GPU 后 cfg 没更新"

    # ③ 保存时以后者为准，不会把开关覆盖回去
    dlg._gpu_chk.setChecked(False)
    dlg._do_save()
    assert w.cfg.ai_gpu is False, "「保存」把刚设的 CPU 覆盖回 GPU 了"


def test_dialog_opening_reflects_menu_state(qapp, monkeypatch, tmp_path):
    """反向同步：菜单里先改了（cfg 已落盘），打开对话框要显示那一份。"""
    dlg, _w = _open_dialog(monkeypatch, tmp_path, gpu=False)
    assert dlg._gpu_chk.isChecked() is False, \
        "cfg.ai_gpu=False，但对话框开关是勾着的"
    assert dlg._device_combo.currentData() == "cpu"


def test_menu_action_still_exists(qapp, monkeypatch, tmp_path):
    """#1：菜单项**保留**（订单：菜单项保留，两处同步）。"""
    dlg, w = _open_dialog(monkeypatch, tmp_path)
    assert w.act_ai_gpu.isCheckable(), "菜单项被删了或不再是可勾选项"
    assert "GPU" in w.act_ai_gpu.text() or "显卡" in w.act_ai_gpu.text()


# ==================== 三、桌面端：「设为当前」即时反馈 ====================

def test_use_model_triggers_immediate_feedback():
    """#3：`_use_model` 必须回调刷新（行高亮/按钮态），不能只 setText。

    行为断言落在「回调有没有被调、带没带那句人话」上 —— 只 grep
    `✓ 当前` 字符串抓不到「回调根本没接上」这类断裂。
    """
    import inspect

    src = inspect.getsource(
        __import__("media_resource_manager.gui.main_window",
                   fromlist=["MainWindow"]).MainWindow._use_model)
    assert "on_done" in src, "_use_model 没有刷新回调参数"
    assert "on_done(" in src, "_use_model 收了回调却没调（还是没反馈）"
    assert "已设为当前" in src, "提示文案不是「已设为当前：…（保存后生效）」"


def test_use_model_callback_receives_confirmation(monkeypatch, tmp_path):
    """真跑一次 _use_model：回调拿到「已设为当前：<名字>（保存后生效）」。"""
    import media_resource_manager.gui.main_window as mw

    got = []

    class _W(mw.MainWindow):
        def __init__(self):
            self.cfg = _Cfg()
            self._logs = []

            class _Sig:
                def emit(_s, msg): self._logs.append(str(msg))
            self.log_signal = _Sig()

        def _models_base_dir(self):
            return Path(tmp_path) / "models"

        def statusBar(self):                      # noqa: N802
            class _SB:
                def showMessage(_s, *_a, **_k): pass
            return _SB()

    class _PI:
        def __init__(self): self._t = ""
        def setText(self, s): self._t = str(s)      # noqa: N802
        def text(self): return self._t

    w = _W()
    w._use_model("clip-vit-b32", _PI(),
                 on_done=lambda msg: got.append(msg), name="CLIP ViT-B/32（内置默认）")
    assert got, "点「设为当前」后没有任何即时反馈回调"
    assert "已设为当前" in got[0] and "保存后生效" in got[0], got
    assert "CLIP ViT-B/32" in got[0], f"提示里没有模型名：{got[0]}"


def test_refresh_use_btns_marks_current_row(qapp, monkeypatch, tmp_path):
    """#3：**真控件**断言 —— 「当前」那一行被标记、按钮变「✓ 当前」并禁用。

    ⚠ 这条必须真跑控件而不是 grep 源码：变异实测证明「把
      `setText("✓ 当前" if is_cur ...)` 改成 `setText("设为当前")`、
      或把 mrmCurrent 恒置 0」都能骗过源码字符串判据，但用户看得见的
      反馈整条消失（订单 #3 就是这个 bug）。
    """
    dlg, _w = _open_dialog(monkeypatch, tmp_path)
    host = getattr(dlg, "_mrm_panel", None)
    assert host is not None, "对话框没挂模型面板"
    btns = getattr(host, "_mrm_use_btns", None)
    rows = getattr(host, "_mrm_use_rows", None)
    assert btns and rows, "模型面板没暴露按钮/行对象（无法做行为级断言）"

    # 造一个「已就绪」的档：真建目录 + 一个够大的 onnx + config.json
    from media_resource_manager.models_lib import model_dir
    base = _w._models_base_dir()
    mid = "clip-vit-b32"          # 内置档，一定可「设为当前」
    d = model_dir(base, mid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "model.onnx").write_bytes(b"\0" * 2048)
    (d / "config.json").write_text("{}", encoding="utf-8")

    # 一开始没有「当前」→ 按钮是普通态
    host._mrm_refresh_use()
    assert btns[mid].text() == "设为当前", btns[mid].text()
    assert rows[mid].property("mrmCurrent") in ("0", 0, None), \
        "还没选就标成当前了"

    # 点「设为当前」（真调 回调）→ 立即变「✓ 当前」+ 禁用 + 行标记 + 提示
    _w._use_model(mid, _find_path_in(dlg),
                  on_done=host._mrm_refresh_use, name="CLIP ViT-B/32")
    assert btns[mid].text() == "✓ 当前", \
        f"点完没变「✓ 当前」：{btns[mid].text()}"
    assert btns[mid].isEnabled() is False, "当前档按钮应禁用（点了没意义）"
    assert str(rows[mid].property("mrmCurrent")) == "1", \
        f"当前行没打高亮标记：{rows[mid].property('mrmCurrent')}"
    st = getattr(host, "_mrm_status", None)
    assert st is not None and "已设为当前" in st.text(), \
        f"状态行没给反馈：{getattr(st, 'text', lambda: '')()}"


def _find_path_in(dlg):
    """在对话框里找那个「本地模型目录」输入框（placeholder 是它唯一的标识）。"""
    from PySide6.QtWidgets import QLineEdit
    for le in dlg.findChildren(QLineEdit):
        if "自带模型" in (le.placeholderText() or ""):
            return le
    raise AssertionError("对话框里找不到本地模型目录输入框")


def test_current_row_style_exists_in_both_palettes():
    """#2：`mrmCurrent` 的样式必须**在两套 QSS 里都有**，且不只靠描边色。

    只换描边色在小屏/浅色皮肤下几乎看不出（R107 的老毛病），
    所以判据要求同时有背景色变化 —— 双通道标记。
    """
    dark = _THEME_TXT.split("DARK_QSS = f\"\"\"", 1)[1].split("\"\"\"", 1)[0]
    light = _THEME_TXT.split("LIGHT_QSS = f\"\"\"", 1)[1].split("\"\"\"", 1)[0]
    for name, seg in (("深色", dark), ("浅色", light)):
        assert 'QFrame#modelCard' in seg, f"{name} QSS 缺 modelCard 基础样式"
        m = re.search(r'QFrame#modelCard\[mrmCurrent="1"\]\s*\{(.*?)\}',
                      seg, re.S)
        assert m, f"{name} QSS 缺「当前行」样式（高亮做不出来）"
        rule = m.group(1)
        assert "background" in rule, f"{name} 当前行只改了描边色（单通道）"
        assert "border" in rule, f"{name} 当前行没有描边"


def test_card_row_uses_foldable_detail_and_inline_progress():
    """#2：门槛收进「详情」折叠 + 进度条**按行**挂（不占全局）。"""
    fn = re.search(r"def _build_model_manager\(.*?\n(?=    def )",
                   _MAIN_TXT, re.S)
    assert fn, "找不到 _build_model_manager"
    body = fn.group(0)
    assert "QToolButton" in body, "没有「详情」折叠按钮"
    assert "_dl_bars[mid] = pbar" in body, \
        "进度条不是按行挂的（订单：只在正在下载那一行内嵌显示）"
    assert body.count("QProgressBar") >= 1
    # 折叠默认收起
    assert "setVisible(False)" in body


# ==================== 四、后端：网页端 GPU 开关与状态 ====================

_TMP = Path(tempfile.mkdtemp(prefix="r172web_"))


def _make_self(cfg):
    """仿 test_r146_part_c 的手法：真 new 一个 _Handler，覆写只读 property。

    ⚠ 不能 new WebServer —— `_api_ai_gpu` / `_api_models` 是 `_Handler` 上的
    方法（R146 起就是这么分的：接口实现挂 Handler，Server 只管起服务）。
    ⚠ `db` / `cfg` 在 _Handler 上是**只读 property**，只能覆写不能赋值。
    """
    from media_resource_manager import webui

    class _H(webui._Handler):
        def __init__(self, cfg):          # 绕过 socket 构造（不调 super）
            self._cfg = cfg

        @property
        def db(self):
            return None

        @property
        def cfg(self):
            return self._cfg

    return _H(cfg)


class _WebCfg:
    ai_gpu = True
    ai_model_path = ""
    ai_backend = "builtin"
    models_dir = ""


@pytest.fixture()
def web_env(monkeypatch):
    monkeypatch.setenv("MRM_HOME", str(_TMP))
    old = os.environ.get("MRM_NO_MIGRATE")
    os.environ["MRM_NO_MIGRATE"] = "1"
    yield
    if old is None:
        os.environ.pop("MRM_NO_MIGRATE", None)
    else:
        os.environ["MRM_NO_MIGRATE"] = old


def test_models_api_exposes_gpu_state(web_env):
    """#1 网页端：`/api/models` 要回过 GPU 开关值 + 实际状态 + 是否降级。

    网页端看不到桌面端菜单，所以「当前到底在用 CPU 还是 GPU」必须由接口
    明确给出；只回一个布尔的话，用户开了 GPU 没显卡还是不知道。
    """
    self_ = _make_self(_WebCfg())
    out = self_._api_models()
    assert out["ok"] is True, out
    for k in ("gpu_want", "gpu_state", "gpu_have", "gpu_degraded"):
        assert k in out, f"/api/models 缺字段 {k}"
    assert out["gpu_want"] is True
    assert out["gpu_state"].strip(), "GPU 状态是空串 —— 网页端没法显示实际后端"
    # 与共享口径一致
    from media_resource_manager.models_lib import device_report
    assert out["gpu_state"] == device_report(True)["text"]


def test_ai_gpu_endpoint_writes_cfg(web_env, monkeypatch):
    """#1 网页端：`/api/ai/gpu` 写的是**同一份 cfg.ai_gpu** 并落盘。"""
    import media_resource_manager.config as cfgmod

    saved = []
    monkeypatch.setattr(cfgmod, "save_config", lambda c: saved.append(c))
    cfg = _WebCfg()
    self_ = _make_self(cfg)

    r = self_._api_ai_gpu({"on": False})
    assert r["ok"] is True, r
    assert cfg.ai_gpu is False, "网页端关掉 GPU 后 cfg 没变"
    assert saved, "网页端改了 GPU 开关却没落盘"
    assert r["gpu_have"] == "cpu"

    r2 = self_._api_ai_gpu({"on": "true"})     # 表单可能送字符串
    assert r2["ok"] is True and cfg.ai_gpu is True, r2


def test_ai_gpu_endpoint_is_routed():
    """路由必须显式字面量（既有护栏按源码字面量判「接没接路由」）。"""
    assert '"/api/ai/gpu"' in _WEBUI_TXT, "webui 没接 /api/ai/gpu 路由"
    assert "def _api_ai_gpu" in _WEBUI_TXT, "没有对应实现"


def test_ai_gpu_endpoint_reports_degraded_text(web_env, monkeypatch):
    """降级时网页端也要拿到「实际用 CPU」这句（否则开关看着像生效了）。"""
    import media_resource_manager.config as cfgmod

    monkeypatch.setattr(cfgmod, "save_config", lambda c: None)
    self_ = _make_self(_WebCfg())
    r = self_._api_ai_gpu({"on": True})
    from media_resource_manager.models_lib import device_report
    assert r["ok"] is True, r
    assert r["gpu_state"] == device_report(True)["text"], r
    # 2026-10-27：本机装了 onnxruntime-directml 之后就有 DmlExecutionProvider 了，
    # 于是"没有显卡后端"这个前提不再成立。按**实际** providers 判：
    #   有 GPU 后端 → 不该报降级；没有 → 必须报降级。
    # ⚠ 2026-11-09 修：判据必须与生产 device_report 同一份名单 ——
    #   AzureExecutionProvider 是云推理、不是本机显卡，生产端把它排除在
    #   GPU 后端之外（models_lib.device_report 的 gpu_backends），测试原来
    #   按「除 CPU 全算 GPU」判，装了 Azure 的机器上假红。
    from media_resource_manager.models_lib import available_providers
    _has_gpu = any(p not in ("CPUExecutionProvider", "AzureExecutionProvider")
                   for p in available_providers())
    assert r.get("gpu_degraded") is (not _has_gpu), (
        f"降级标记与本机后端不一致：providers={available_providers()} "
        f"degraded={r.get('gpu_degraded')}")


# ==================== 五、前端：卡片行四态（node 真跑） ====================

def _node(probe: str):
    import json
    import subprocess

    prelude = (
        "// R172：只抽需要的函数真跑（app.js 顶层有 document 依赖，"
        "不能整个 require）\n"
        "function esc(s) {\n"
        "  return String(s === null || s === undefined ? '' : s)\n"
        "    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')\n"
        "    .replace(/\"/g,'&quot;').replace(/'/g,'&#39;');\n"
        "}\n"
        "var MODEL_STATE_TEXT = {current:'使用中', ready:'已就绪',"
        " builtin:'已内置', download:'未下载'};\n"
    )
    if isinstance(probe, (list, tuple)):
        probe = "\n".join(probe)
    p = subprocess.run(["node", "-e", prelude + probe],
                       capture_output=True, text=True, timeout=60,
                       encoding="utf-8", errors="replace")
    assert p.returncode == 0, f"node 失败：{p.stderr}"
    out = (p.stdout or "").strip().splitlines()
    assert out, "node 没输出"
    return json.loads(out[-1])


def _fn_src(name: str) -> str:
    """抽 app.js 顶层 `function name(...) {...}`（去注释）。"""
    m = re.search(r"^function %s\([^)]*\)\s*\{" % re.escape(name),
                  _APPJS_TXT, re.M)
    assert m, f"app.js 找不到函数 {name}"
    i = m.start()
    j = _APPJS_TXT.index("\n}", i) + 2
    return re.sub(r"/\*.*?\*/", "", _APPJS_TXT[i:j], flags=re.S)


def _card_probe(m: str):
    return [_fn_src("mrmModelState"), _fn_src("mrmModelCardHtml"),
            "console.log(JSON.stringify(mrmModelCardHtml(%s, 0)));" % m]

# ==================== 六、两端口径一致性（防漂移） ====================

def test_both_ends_share_model_state_semantics():
    """桌面「✓ 当前」与网页「✓ 当前」是同一套语义（都按磁盘路径判定）。

    网页端 `_api_models` 用 `Path(cur).resolve() == path.resolve()` 判 current；
    桌面端 `_refresh_use_btns` 用 `_model_dir_resolved(mid) == want`。
    两边都必须是**路径**判定 —— 按名字猜会在改名/多档同名前功尽弃。
    """
    assert "resolve()" in _WEBUI_TXT.split("def _api_models", 1)[1][:3000]
    fn = re.search(r"def _refresh_use_btns\(.*?\n(?=    def )", _MAIN_TXT, re.S)
    assert fn and "_model_dir_resolved" in fn.group(0), \
        "桌面端不是按解析后的目录判定「当前」"
