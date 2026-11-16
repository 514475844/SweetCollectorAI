"""R161 护栏：R152 的 C1 / D2 / D6 / E3 四条（执行方笔记本棒）。

每条都按「行为断言优先、源码判据兜底」配，变异自检脚本
.workbuddy/_r161_mutate.py 逐条注入坏实现验证会被抓。

- C1 适配器名路径穿越：load_adapter 出口 sanitize + engine._resolve_adapter
  白名单拒绝（与 Web 端 R144 导入闸同源 ^[A-Za-z0-9_-]{1,64}$）。
- D2 learn_folder 静默降级：传 cfg.ai_model_path + 非 clip 后端不打标、
  stats.ai_note 明说（原来 stub 读边车全打 1.0 = 假标签入库）。
- D6 stats 误桶：`"ai" in s` 把 domain/email 判成"本地AI"，改词界+标记。
- E3 登录限流：按 IP 失败计数 + 指数退避（429+Retry-After），
  成功清零；失败写 db.audit；自动密码 48bit→128bit。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import types
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ---- 隔离必须在 import config 之前（config.user_data_dir 读 MRM_HOME） ----
_TMP = Path(tempfile.mkdtemp(prefix="r161test_"))


@pytest.fixture()
def web_home():
    """给每个用例一个干净的 MRM_HOME，退出时还原（R67 教训：必须 finally）。"""
    old = {k: os.environ.get(k) for k in ("MRM_HOME", "MRM_NO_MIGRATE")}
    os.environ["MRM_HOME"] = str(_TMP)
    os.environ["MRM_NO_MIGRATE"] = "1"
    try:
        yield _TMP
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _write_rule(p: Path, name: str) -> Path:
    p.write_text(
        f"name: {name}\nbase_url: https://example.com\n"
        f"list_page_selectors: {{}}\n",
        encoding="utf-8")
    return p


# ==================== C1 · 适配器名路径穿越 ====================

def test_c1_load_adapter_sanitizes_traversal_name(tmp_path):
    """yaml 里 name 带路径穿越字符 → load_adapter 出口回退到文件名 stem。"""
    from media_resource_manager.crawler.rules import load_adapter
    f = _write_rule(tmp_path / "goodsit.yaml", "../../etc/evil")
    rule = load_adapter(f)
    assert rule.name == "goodsit", rule.name
    # 名字不再含任何路径字符（它会被拼进下载目录名）
    assert "/" not in rule.name and "\\" not in rule.name
    assert ".." not in rule.name


def test_c1_load_adapter_keeps_valid_name(tmp_path):
    """白名单内的名字原样通过（不误伤正常规则）。"""
    from media_resource_manager.crawler.rules import load_adapter
    f = _write_rule(tmp_path / "a.yaml", "gb84791rx")
    assert load_adapter(f).name == "gb84791rx"


def test_c1_load_adapter_bad_stem_falls_back_adapter(tmp_path):
    """文件名本身也不合法（手工塞的）→ 收敛到 "adapter"，绝不把坏名放出去。"""
    from media_resource_manager.crawler.rules import load_adapter
    f = _write_rule(tmp_path / "坏名字.yaml", "也坏")
    rule = load_adapter(f)
    assert rule.name == "adapter", rule.name


def test_c1_resolve_adapter_rejects_traversal(tmp_path):
    """engine._resolve_adapter 对不合法规名一律 None（读也穿越不了）。"""
    from media_resource_manager.engine import Engine
    eng = object.__new__(Engine)           # 跳过重型 __init__
    rules = tmp_path / "rules"
    rules.mkdir()
    _write_rule(rules / "ok.yaml", "ok")
    eng.cfg = types.SimpleNamespace(rules_dir=rules)
    # 正当名字照旧解析
    assert eng._resolve_adapter("ok") == rules / "ok.yaml"
    # 穿越/畸形名字全部拒绝（哪怕磁盘上真存在对应文件）
    for bad in ("../../etc/passwd", "..\\..\\evil", "ok/../evil",
                "ok/", ".", "..", "ok yaml", "ok.yaml"):
        assert eng._resolve_adapter(bad) is None, bad


def test_c1_resolve_adapter_rejects_existing_traversal_target(tmp_path):
    """★关键行为：穿越目标文件**真存在**时也必须拒（否则白名单形同虚设）。

    变异自检 M2 抓到过：只断言"不存在时返回 None"的话，把白名单检查
    整个删掉、回到直拼路径，测试照样绿——因为拼出来的路径本来就没有
    对应文件。必须让 ../evil.yaml 真实存在于 rules_dir 外。
    """
    from media_resource_manager.engine import Engine
    eng = object.__new__(Engine)
    rules = tmp_path / "rules"
    rules.mkdir()
    _write_rule(tmp_path / "evil.yaml", "../../evil")   # 在 rules_dir 外
    _write_rule(rules / "ok.yaml", "ok")
    eng.cfg = types.SimpleNamespace(rules_dir=rules)
    # 坏实现（直拼路径）会解析到 tmp_path/evil.yaml；好实现返回 None
    assert eng._resolve_adapter("../evil") is None
    assert eng._resolve_adapter("../../evil") is None
    # 正当名字不受影响
    assert eng._resolve_adapter("ok") == rules / "ok.yaml"


def test_c1_source_guard_whitelist_at_both_exits():
    """源码判据：rules.py 出口有 sanitize，engine 有白名单拒绝。"""
    rules_src = (ROOT / "media_resource_manager" / "crawler" / "rules.py"
                 ).read_text(encoding="utf-8")
    assert "ADAPTER_NAME_OK" in rules_src, "rules.py 缺白名单"
    assert "sanitize_adapter_name(path.stem)" in rules_src, \
        "load_adapter 出口没把 name 过 sanitize"
    eng_src = (ROOT / "media_resource_manager" / "engine.py"
               ).read_text(encoding="utf-8")
    idx = eng_src.find("def _resolve_adapter")
    assert idx != -1
    block = eng_src[idx:idx + 700]
    assert "ADAPTER_NAME_OK.match" in block, "_resolve_adapter 没过白名单"


# ==================== D2 · learn_folder 静默降级 ====================

def _lf_setup(tmp_path):
    from media_resource_manager.config import AppConfig
    from media_resource_manager.db import Database
    cfg = AppConfig(db_path=tmp_path / "mrm.db",
                    library_dir=tmp_path / "lib",
                    rules_dir=tmp_path / "rules",
                    isolate_dir=tmp_path / "quarantine",
                    log_dir=tmp_path / "logs")
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    folder = tmp_path / "my_album"
    folder.mkdir()
    from PIL import Image
    im = Image.new("RGB", (40, 30), (120, 80, 60))
    im.save(folder / "p0.jpg")
    return cfg, db, folder


def test_d2_stub_backend_skips_tagging_and_says_so(tmp_path, monkeypatch):
    """非 clip 后端：不打标（stub 会打假标签），stats.ai_note 明说降级。"""
    from media_resource_manager.learn_folder import learn_folder
    cfg, db, folder = _lf_setup(tmp_path)
    cfg.ai_model_path = tmp_path / "models"      # 显式模型目录
    seen: dict = {}

    class _FakeStub:
        def __init__(self, labels=None, model_dir=None, threads=0, gpu=True):
            seen["labels"] = labels
            seen["model_dir"] = model_dir
            self.backend = "stub"

        def classify(self, p):
            return [("cat", 1.0), ("sunset", 1.0)]   # stub 行为：边车全 1.0

    monkeypatch.setattr(
        "media_resource_manager.classify.clip_model.LocalClassifier", _FakeStub)
    s = learn_folder(db, cfg, str(folder))
    # 模型目录真的传进去了（原来压根不传）
    assert seen["model_dir"] == cfg.ai_model_path, seen
    # 降级被明说，不是静默
    assert "降级" in s.get("ai_note", ""), s
    # 假标签没有入库（tags 以 JSON 字符串存储，空 = "[]"）
    rows = db.query("SELECT tags FROM images")
    assert rows and all(not json.loads(r["tags"] or "[]") for r in rows), rows


def test_d2_clip_backend_tags_normally(tmp_path, monkeypatch):
    """clip 后端：正常打标，ai_note 为空（不误报降级）。"""
    from media_resource_manager.learn_folder import learn_folder
    cfg, db, folder = _lf_setup(tmp_path)
    seen: dict = {}

    class _FakeClip:
        def __init__(self, labels=None, model_dir=None, threads=0, gpu=True):
            seen["model_dir"] = model_dir
            self.backend = "clip"

        def classify(self, p):
            return [("cat", 0.9), ("dog", 0.1)]

    monkeypatch.setattr(
        "media_resource_manager.classify.clip_model.LocalClassifier", _FakeClip)
    s = learn_folder(db, cfg, str(folder))
    assert s.get("ai_note", "") == "", s
    assert seen["model_dir"] is None  # 没配 ai_model_path 时传 None（走内置解析）
    rows = db.query("SELECT tags FROM images")
    assert any(json.loads(r["tags"] or "[]") for r in rows), rows


def test_d2_source_guard_backend_check_present():
    src = (ROOT / "media_resource_manager" / "learn_folder.py"
           ).read_text(encoding="utf-8")
    assert 'getattr(clf, "backend", "") != "clip"' in src, "缺 backend 检查"
    assert "ai_note" in src, "缺降级记账"


# ==================== D6 · stats 误桶 ====================

def test_d6_domain_email_not_counted_as_ai():
    """domain / email / detail 里的 "ai" 子串不再误判"本地AI"。"""
    from media_resource_manager.stats import failure_reasons
    got = failure_reasons([
        "domain resolution failed for img.example.com",
        "email notification broken",
        "detail: fetch said 404",
    ])
    assert "本地AI" not in got, got
    assert got.get("其他") == 3, got


def test_d6_real_ai_errors_still_bucketed():
    """真 AI 错误照旧进"本地AI"桶（中英文两种口径）。"""
    from media_resource_manager.stats import failure_reasons
    got = failure_reasons([
        "local AI unavailable: onnxruntime missing",
        "本地 AI 处于降级模式（后端=stub）",
        "clip model load failed",
    ])
    assert got.get("本地AI") == 3, got


def test_d6_source_guard_no_substring_ai():
    src = (ROOT / "media_resource_manager" / "stats.py"
           ).read_text(encoding="utf-8")
    idx = src.find("def failure_reasons")
    assert idx != -1
    block = src[idx:idx + 900]
    assert '"ai" in s' not in block, "failure_reasons 里又有裸 ai 子串判据"


# ==================== E3 · 登录限流 ====================

class _Client:
    def __init__(self, base: str) -> None:
        self.base = base
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def post(self, path: str, body: dict):
        data = json.dumps(body).encode("utf-8")
        r = urllib.request.Request(
            self.base + path, data=data, method="POST",
            headers={"Content-Type": "application/json",
                     "Accept": "application/json"})
        try:
            with self.opener.open(r, timeout=15) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)


@pytest.fixture()
def auth_server(web_home):
    """真起 WebServer：web_lan_open=False + 白名单空 ⇒ 逼真走密码校验。"""
    from media_resource_manager import webui
    from media_resource_manager.config import AppConfig
    from media_resource_manager.db import Database

    cfg = AppConfig()
    cfg.db_path = str(web_home / "auth.db")
    cfg.web_password = "pw123"
    cfg.web_bind = "127.0.0.1"
    cfg.web_port = 0
    cfg.web_lan_open = False
    cfg.web_ip_whitelist = []

    import media_resource_manager.config as cfgmod
    orig_save = cfgmod.save_config
    cfgmod.save_config = lambda *a, **k: None

    db = Database(web_home / "auth.db")
    srv = webui.WebServer(cfg, db, queue=object())
    ok, msg = srv.start()
    assert ok, f"服务器起不来：{msg}"
    try:
        yield cfg, db, _Client(f"http://127.0.0.1:{cfg.web_port}")
    finally:
        srv.stop()
        db.close()
        cfgmod.save_config = orig_save


def test_e3_lockout_after_max_fails(auth_server):
    """连续错到阈值 → 429 + Retry-After；锁定期内正确密码也进不来。"""
    cfg, db, cli = auth_server
    from media_resource_manager.webui import _LOGIN_MAX_FAILS
    for i in range(_LOGIN_MAX_FAILS):
        st, raw, _ = cli.post("/api/login", {"password": "wrong"})
        assert st == 401, (i, st, raw[:80])
    st, raw, hdrs = cli.post("/api/login", {"password": "wrong"})
    assert st == 429, (st, raw[:120])
    assert "秒后再试" in raw.decode("utf-8")
    assert "Retry-After" in hdrs, hdrs
    # 锁定期内连正确密码都拒（爆破必须停）
    st, raw, _ = cli.post("/api/login", {"password": "pw123"})
    assert st == 429, (st, raw[:120])


def test_e3_success_resets_counter(auth_server):
    """失败未达阈时登录成功 → 计数清零，不会被历史失败连坐。"""
    cfg, db, cli = auth_server
    from media_resource_manager.webui import _LOGIN_MAX_FAILS
    for _ in range(_LOGIN_MAX_FAILS - 1):
        st, _, _ = cli.post("/api/login", {"password": "wrong"})
        assert st == 401
    st, raw, _ = cli.post("/api/login", {"password": "pw123"})
    assert st == 200 and json.loads(raw).get("ok") is True, (st, raw[:120])
    # 清零后还能再错满一整轮才锁（证明真的清零了：没清零的话第 5 次
    # 累计失败就会锁，下一次正确密码也进不来）。
    # ⚠ 第 _LOGIN_MAX_FAILS 次失败是"触发锁定"的那次，本次仍回 401，
    #   下一次才 429 —— 别在这里数错一位。
    for i in range(_LOGIN_MAX_FAILS):
        st, _, _ = cli.post("/api/login", {"password": "wrong"})
        assert st == 401, (i, st)
    st, _, _ = cli.post("/api/login", {"password": "wrong"})
    assert st == 429


def test_e3_failure_written_to_audit(auth_server):
    """失败登录写 db.audit_log（桌面端安全页可翻）。"""
    cfg, db, cli = auth_server
    cli.post("/api/login", {"password": "wrong"})
    rows = db.query(
        "SELECT actor, action, target FROM audit_log WHERE action='login-failed'")
    assert rows, "失败登录没有审计记录"
    assert rows[0]["actor"] == "web"


def test_e3_backoff_is_exponential(monkeypatch):
    """单元级：锁定时长 30s → 60s → … 封顶 900s（不真等，直接读状态）。"""
    from media_resource_manager import webui
    h = object.__new__(webui._Handler)
    h.server = types.SimpleNamespace(
        _login_fails={}, _login_lock=threading.Lock(),
        cfg=types.SimpleNamespace(trust_proxy=False))
    h.client_address = ("10.9.9.9", 5555)
    now = time.monotonic()
    for i in range(1, 8):
        n = h._login_failed()
        assert n == i, (i, n)
        rec = h.server._login_fails["10.9.9.9"]
        if i < webui._LOGIN_MAX_FAILS:
            assert rec["until"] == 0.0 or rec["until"] <= now, rec
        else:
            over = i - webui._LOGIN_MAX_FAILS
            # 2026-10-27（编号 26）：锁定时长改成可配（web_login_ban_min，分钟）。
            # 替身 server.cfg 上没有这两个键 ⇒ 走代码里的默认值（5 次 / 10 分钟）。
            # 基准 = max(配置分钟×60, 旧的 30 秒)，再按 2^over 翻倍、封顶 15 分钟。
            _max = int(getattr(h.server.cfg, "web_login_max_fail", 5) or 5)
            _ban = float(getattr(h.server.cfg, "web_login_ban_min", 10) or 10) * 60.0
            assert webui._LOGIN_MAX_FAILS == 5, "代码默认阈值不该被改掉"
            want = min(max(_ban, webui._LOGIN_BACKOFF_BASE) * (2 ** over),
                       webui._LOGIN_BACKOFF_CAP)
            got = rec["until"] - rec["last"]
            assert abs(got - want) < 0.5, (i, got, want)
    # 成功清零
    h._login_succeeded()
    assert "10.9.9.9" not in h.server._login_fails


def test_e3_gate_opens_after_window(monkeypatch):
    """锁定窗口过后 gate 放行（时间单调推进，不真睡）。"""
    from media_resource_manager import webui
    h = object.__new__(webui._Handler)
    h.server = types.SimpleNamespace(
        _login_fails={"10.8.8.8": {"count": 9, "last": time.monotonic() - 10**6,
                                   "until": time.monotonic() - 5}},
        _login_lock=threading.Lock(),
        cfg=types.SimpleNamespace(trust_proxy=False))
    h.client_address = ("10.8.8.8", 5555)
    assert h._login_gate() == 0.0


def test_e3_auto_password_is_128bit():
    """E3 遗留护栏（R157 后已**部分过期**，如实标注）。

    原判据：`WebServer.start()` 未设密码时会自动生成 `token_hex(16)`（128bit）
    并持久化，此条钉住「不许回退到 token_hex(6) 的 48bit」。

    ⚠⚠ R157（2026-10-19 用户裁决）已把「自动生成密码」整条路径**删除** ——
    空密码现在是免密模式，不生成、不保存。所以本条的原始语义（比对自动密码
    的位数）**已无对象可测**，真实契约收敛成一句：源码里不许再出现自动生成
    密码的 `token_hex(...)` 调用。

    ⚠ 只有**代码**（去注释后）才作数：R157 特意在注释里保留了
    `secrets.token_hex(16)` 作为历史说明，光搜字符串会让这条变成假绿
    （R63-D3 同型坑：注释骗过护栏）。
    """
    import re
    src = (ROOT / "media_resource_manager" / "webui.py"
           ).read_text(encoding="utf-8")
    # 逐行剥掉 `#` 注释（R157 的说明写在注释里，不能算代码）
    code = "\n".join(ln.split("#", 1)[0] for ln in src.splitlines())
    assert not re.search(r"secrets\.token_hex\s*\(", code), \
        "源码里仍有自动生成密码的调用 —— R157 后应为免密模式，不再生成密码"
