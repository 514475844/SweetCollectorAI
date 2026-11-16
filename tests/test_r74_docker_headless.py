# -*- coding: utf-8 -*-
"""R104 · 护栏测试 tests/test_r74_docker_headless.py —— DOCKER_X86_ORDER §2 #7。

三条设计口径（改之前先读）：
  ① **全程不需要 docker** —— F1–F9 那九项要真起容器，不属于单元测试；
     这里只测"代码形态 + 纯函数逻辑"，保证没装 docker 的机器也能跑。
  ② headless.py / Dockerfile / compose 用 importorskip 与 skipif 守卫 ⇒
     未落地时自动跳过，**不会把主干跑红**；落地后自动生效，不用回来改测试。
  ③ 断言失败信息一律中文；异常路径一律不抛（沿用 _ip_is_private 的口径）。

已按真实情况修订（R104 落地时）：
  · test_dockerfile_shape 增加 HEALTHCHECK 必须探 /healthz（鉴权前放行）、
    不许探 /api/session（鉴权后，安全姿态下必 401 ⇒ 永久 unhealthy）。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest


def _repo_root() -> Path:
    """仓库根：**靠特征文件往上找**，不要用 parents[1] 硬推。

    R102 实测踩过：把测试放到临时目录里跑时 parents[1] 指到了别处，
    于是 `_read()` 静默返回空串 ⇒ 三条断言全假红（"找不到路径"其实是"找错根目录"）。
    """
    p = Path(__file__).resolve()
    for cand in (p.parent, *p.parents):
        if (cand / "media_resource_manager" / "webui.py").exists():
            return cand
    return p.parents[1]


REPO_ROOT = _repo_root()


# ───────────────────────── E1：真实客户端 IP ─────────────────────────
def _fake_handler(client_ip: str, headers: dict, trust_proxy: bool):
    """造一个只挂了 _client_ip 所需属性的 _Handler。

    两处必须绕开（R102 实测踩的）：
      · 不跑 __init__：它继承 BaseHTTPRequestHandler，构造要真 socket；
      · **`cfg` 是只读 property**（webui.py:381-383 → self.server.cfg），
        `h.cfg = ...` 直接 AttributeError。所以挂 `h.server` 让 property 自己解析，
        顺带把"cfg 从 server 来"这条真实链路也测到。
    """
    from media_resource_manager.webui import _Handler

    h = _Handler.__new__(_Handler)
    h.client_address = (client_ip, 54321)
    h.headers = headers
    h.server = types.SimpleNamespace(
        cfg=types.SimpleNamespace(trust_proxy=trust_proxy))
    return h


def _client_ip(h):
    from media_resource_manager.webui import _Handler

    return _Handler._client_ip(h)


def test_client_ip_ignores_proxy_headers_by_default():
    """默认不信任代理头：伪造 XFF 也拿不到免密（安全默认，订单 E1）。"""
    h = _fake_handler(
        "172.17.0.1",
        {"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1"},
        trust_proxy=False,
    )
    assert _client_ip(h) == "172.17.0.1", "默认必须忽略可伪造的代理头"


def test_client_ip_trusts_proxy_when_enabled():
    """trust_proxy=True：优先 X-Real-IP，其次 X-Forwarded-For 最左一跳。"""
    h = _fake_handler(
        "172.17.0.1",
        {"X-Forwarded-For": "203.0.113.9, 172.17.0.1", "X-Real-IP": "203.0.113.9"},
        trust_proxy=True,
    )
    assert _client_ip(h) == "203.0.113.9", "开启后应取真实客户端 IP"

    h2 = _fake_handler(
        "172.17.0.1",
        {"X-Forwarded-For": "203.0.113.9, 172.17.0.1"},
        trust_proxy=True,
    )
    assert _client_ip(h2) == "203.0.113.9", "无 X-Real-IP 时应取 XFF 最左一跳"


def test_client_ip_falls_back_to_direct_on_junk_headers():
    """头是垃圾/异常时退回直连，绝不 500（宁严勿松）。"""
    h = _fake_handler("10.0.0.7", {}, trust_proxy=True)
    assert _client_ip(h) == "10.0.0.7", "代理头缺失时应退回直连地址"


def test_config_trust_proxy_defaults_to_false():
    """AppConfig.trust_proxy 默认必须是 False（安全默认；R103 运行时断言过）。"""
    from media_resource_manager.config import AppConfig

    cfg = AppConfig()
    assert getattr(cfg, "trust_proxy", None) is False, (
        "trust_proxy 默认必须为 False——代理头可伪造，默认信任=全网免密")
    d = cfg.to_dict()
    assert d.get("trust_proxy") is False, "to_dict 必须带上 trust_proxy（否则重启丢失）"


# ───────────────────────── A1：无头链路不许拖进 PySide6 ─────────────
def test_headless_import_does_not_load_pyside6():
    """子进程隔离验证：import 无头入口后 sys.modules 里不许出现 PySide6。

    用子进程而不是直接 import：主进程可能已被 conftest / 别的测试带进 PySide6，
    那样这条断言永远假绿。
    """
    headless = pytest.importorskip("media_resource_manager.headless")
    del headless
    code = (
        "import sys;"
        "import media_resource_manager.headless;"   # noqa: E702
        "bad=[m for m in sys.modules if m.split('.')[0]=='PySide6'];"
        "print('BAD:' + ','.join(bad) if bad else 'OK');"
        "sys.exit(1 if bad else 0)"
    )
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", code],
        capture_output=True, text=True, timeout=180,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, (
        "无头入口把 PySide6 拖进了进程（容器里必然 ImportError）："
        f"{proc.stdout.strip()}{proc.stderr.strip()[-400:]}"
    )


def test_webui_theme_path_does_not_load_pyside6():
    """R112：只 import headless 是**不够的**——真正的坑在**运行时**。

    webui 在请求路径上 `from .gui import theme` 取皮肤调色板；而 Python 导入
    `pkg.sub` 必定先执行 `pkg/__init__.py`。R112 实测：gui/__init__ 在模块层
    import main_window ⇒ 无头链路把 PySide6 拖进进程（27 处，见
    .workbuddy/r112_pyside_import_chain.txt）。容器里没有 PySide6 ⇒
    webui 的 try/except 会兜住，于是**静默降级**成 3 个硬编码皮肤 + 空强调色，
    比崩掉更难查。这里把"取到真预设"也一并断言，防的就是这种假绿。
    """
    pytest.importorskip("media_resource_manager.webui")
    code = (
        "import sys;"
        "import media_resource_manager.webui as w;"
        "pres=w._theme_presets();"
        "w._preset_label_safe('violet_look');"
        "w._fallback_accent();"
        "w.web_theme_payload('violet_look');"
        "bad=[m for m in sys.modules if m.split('.')[0]=='PySide6'];"
        "print('PRESETS:' + ','.join(pres));"
        "print('BAD:' + ','.join(bad) if bad else 'OK-PYSIDE6-CLEAN');"
        "sys.exit(1 if bad or len(pres) < 4 else 0)"
    )
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", code],
        capture_output=True, text=True, timeout=180,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, (
        "Web 取皮肤的路径把 PySide6 拖进了进程，或只拿到兜底预设（静默降级）："
        f"{proc.stdout.strip()}{proc.stderr.strip()[-400:]}"
    )
    assert "OK-PYSIDE6-CLEAN" in proc.stdout, proc.stdout[:300]


def test_gui_package_import_is_lazy():
    """R112：`import media_resource_manager.gui` 本身不许拉起 main_window。

    gui/__init__ 走 PEP 562 惰性属性 ⇒ 只 import 包时不应出现 PySide6；
    `from .gui import launch` 才真正导入（下面正判据另测，避免恒真的负对照）。
    """
    code = (
        "import sys;"
        "import media_resource_manager.gui;"
        "bad=[m for m in sys.modules if m.split('.')[0]=='PySide6'];"
        "print('BAD:' + ','.join(bad) if bad else 'OK');"
        "sys.exit(1 if bad else 0)"
    )
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", code],
        capture_output=True, text=True, timeout=180,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, (
        "gui 包在模块层就把 PySide6 拉起来了（无头/容器必炸或静默降级）："
        f"{proc.stdout.strip()}{proc.stderr.strip()[-400:]}"
    )


def test_gui_launch_still_importable():
    """正判据（防上一条把惰性改成永久不可用）：`from .gui import launch` 仍要能拿到。"""
    gui = pytest.importorskip("media_resource_manager.gui")
    assert callable(gui.launch), "gui.launch 不可调用（惰性 __getattr__ 写坏了）"
    assert gui.MainWindow is not None, "gui.MainWindow 取不到（惰性 __getattr__ 写坏了）"


# R130：上面三条都是「本机装了 PySide6 时看它有没有被 import」——属于**负判据**，
# 证明不了容器里（根本没装 PySide6）能跑起来。requirements-docker.txt 明确不装
# PySide6，所以真正的验收口径是：**把 PySide6 从解释器里"摘掉"，无头主循环
# 还能不能完整跑完并优雅退出**。下面这条就是那个口径。
_R130_NO_PYSIDE_SNIPPET = r'''
import os, signal, sys, tempfile, threading, traceback

class _Blocker:
    hits = []
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in ("PySide6", "shiboken6"):
            _Blocker.hits.append(
                fullname + " <- " + " <- ".join(
                    f"{fr.filename}:{fr.lineno}:{fr.name}"
                    for fr in traceback.extract_stack()[-4:-1]))
            raise ImportError("模拟容器环境：未安装 " + fullname)
        return None

sys.meta_path.insert(0, _Blocker())
os.environ["MRM_HOME"] = tempfile.mkdtemp(prefix="mrm_r130_guard_")
os.environ.update(MRM_NO_MIGRATE="1", MRM_WEB_LAN_OPEN="0",
                  MRM_WEB_PASSWORD="r130guard", MRM_INTERVAL="2")
from media_resource_manager import headless

# 必须在**主线程**跑 headless：signal.signal 非主线程会抛 ValueError 而被
# headless 静默吞掉 ⇒ SIGTERM 处理器根本没注册，那就测不出优雅退出（假绿）。
threading.Thread(target=lambda: (threading.Event().wait(5.0),
                                 signal.raise_signal(signal.SIGTERM)),
                 daemon=True).start()
try:
    rc = headless.headless_main(["--no-web", "--no-ai"])
except BaseException as e:
    print("EXC:" + type(e).__name__ + ":" + str(e))
    print(traceback.format_exc()[-1500:])
    print("HITS:" + "; ".join(_Blocker.hits[:3]))
    sys.exit(1)
print("RC:%r" % (rc,))
print("HITS:" + "; ".join(_Blocker.hits[:3]) if _Blocker.hits else "HITS:0")
sys.exit(0 if (rc == 0 and not _Blocker.hits) else 1)
'''


def test_headless_main_survives_missing_pyside6():
    """R130：模拟"容器里没装 PySide6"，无头主循环必须能起来并优雅退出。

    与上面三条的差别（这是本条存在的理由）：
      · 那三条是**负判据**（本机装了 PySide6，看有没有被 import）；
      · 这条是**正判据**（把 PySide6 摘掉，看主循环还跑不跑得完）。
    只断言"没 import"是不够的：假如有人把某个模块写成
    `try: from PySide6 import X except ImportError: X = None`，负判据照样绿，
    而容器里那条路径会静默降级（R112 就踩过 webui 皮肤兜底）。

    正判据还顺带覆盖了：SIGTERM 处理器确实注册并让主循环退出（A3）、
    收尾没把异常抛到顶层（§1.4 不得硬崩）。
    """
    pytest.importorskip("media_resource_manager.headless")
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", _R130_NO_PYSIDE_SNIPPET],
        capture_output=True, text=True, timeout=180, cwd=str(REPO_ROOT),
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0, (
        "无头入口在「未安装 PySide6」的环境下跑不通（容器必炸或静默降级）：\n"
        + out[-1200:]
    )
    assert "HITS:0" in out, (
        "无头链路尝试过 import PySide6（本机装了所以没崩，容器里必 ImportError）：\n"
        + out[-1200:]
    )


# ───────────────────────── A4：环境变量覆盖配置 ─────────────────────
def test_env_overrides_basic_types(monkeypatch):
    """MRM_* 覆盖 cfg：bool / int / list 三种类型转换都对。"""
    headless = pytest.importorskip("media_resource_manager.headless")
    from media_resource_manager.config import AppConfig

    monkeypatch.setenv("MRM_WEB_BIND", "0.0.0.0")
    monkeypatch.setenv("MRM_WEB_PORT", "8080")
    monkeypatch.setenv("MRM_WEB_LAN_OPEN", "0")
    monkeypatch.setenv("MRM_WEB_IP_WHITELIST", "10.8.0.0/24, 203.0.113.5")

    cfg = AppConfig()
    applied = headless.apply_env_overrides(cfg)

    assert cfg.web_bind == "0.0.0.0", "容器必须能改监听地址（默认 127.0.0.1 连不进来）"
    assert cfg.web_port == 8080 and isinstance(cfg.web_port, int)
    assert cfg.web_lan_open is False, "字符串 '0' 必须转成 False"
    assert cfg.web_ip_whitelist == ["10.8.0.0/24", "203.0.113.5"]
    assert any("web_bind" in a for a in applied)


def test_env_override_bad_int_reports_chinese(monkeypatch):
    """非法值必须中文报错，不许抛英文 traceback（§1.4）。"""
    headless = pytest.importorskip("media_resource_manager.headless")
    from media_resource_manager.config import AppConfig

    monkeypatch.setenv("MRM_WEB_PORT", "不是数字")
    with pytest.raises(SystemExit) as ei:
        headless.apply_env_overrides(AppConfig())
    msg = str(ei.value)
    assert msg and any("\u4e00" <= ch <= "\u9fff" for ch in msg), (
        f"错误信息必须是中文：{msg!r}"
    )


# ───────────────────────── C/D：镜像与编排形态（不真起容器） ─────────
def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _dockerfile_instructions(text: str) -> str:
    """剥掉 Dockerfile 的注释行再判。

    R104 实测：头部注释里写一句"故意不用 `pip install .`"就会让
    `pip install .` 的字面判据**假红**——R63-D3「判据被注释/docstring 骗到」
    同型复发。⇒ 判指令一律先剥注释，别去改注释措辞（下一句注释还会踩）。
    """
    return "\n".join(ln for ln in text.splitlines()
                     if not ln.lstrip().startswith("#"))


def _healthcheck_block(text: str) -> str:
    """切出 HEALTHCHECK 指令块（含反斜杠续行），用于精确判据。"""
    lines = text.splitlines()
    out: list[str] = []
    in_hc = False
    for ln in lines:
        if ln.startswith("HEALTHCHECK"):
            in_hc = True
        if in_hc:
            out.append(ln)
            if not ln.rstrip().endswith("\\"):
                break
    return "\n".join(out)


@pytest.mark.skipif(not (Path(__file__).resolve().parents[1] / "Dockerfile").exists(),
                    reason="Dockerfile 尚未落地")
def test_dockerfile_shape():
    """C1/C2/C4/C5/C6：多阶段、glibc 基础镜像、健康探活、非 root、时区。"""
    text = _read(REPO_ROOT / "Dockerfile")
    assert "python:3.11-slim" in text, "C2 要求 Debian/glibc，不许 alpine/musl"
    assert "AS builder" in text, "C1 要求多阶段构建"
    assert "HEALTHCHECK" in text, "C4 要求探活"
    assert "USER 1000" in text, "B5 要求非 root 运行"
    assert "Asia/Shanghai" in text, "C6 要求时区（中文文件名不许乱码）"
    assert "headless.py" in text, "A1：无头入口必须随源码树进镜像（构建期断言）"
    assert re.search(r"MRM_WEB_BIND\s*=\s*0\.0\.0\.0", text), (
        "R130：镜像里必须把监听地址改成 0.0.0.0。程序默认 127.0.0.1，"
        "容器里不改的话 `docker run -p 8080:8080` 宿主连不进，而 HEALTHCHECK 在"
        "容器内打 127.0.0.1 照样绿 ⇒ 容器 healthy 但页面打不开（假绿）")


@pytest.mark.skipif(not (Path(__file__).resolve().parents[1] / "Dockerfile").exists(),
                    reason="Dockerfile 尚未落地")
def test_healthcheck_probes_auth_exempt_endpoint():
    """★ R104 新增：探活必须打 /healthz（鉴权前放行），不许打 /api/session。

    /api/session 在 `if not self._auth_ok(): return self._unauthorized()`
    **之后**；本套件安全姿态是 MRM_WEB_LAN_OPEN=0 + 必须设密码 ⇒ 不带凭证的
    /api/session 恒 401 ⇒ 容器被永久判 unhealthy（F2/F7 全假绿）。
    /healthz 是 R50 专门为探活建的端点（鉴权之前、只回 {"ok": true}）。
    """
    text = _read(REPO_ROOT / "Dockerfile")
    hc = _healthcheck_block(text)
    assert hc, "Dockerfile 里没有 HEALTHCHECK 指令"
    assert "/healthz" in hc, "探活必须打鉴权前放行的 /healthz"
    assert "/api/session" not in hc, (
        "探活不许打 /api/session：它在鉴权之后，安全姿态下必 401 ⇒ 永久 unhealthy")


@pytest.mark.skipif(not (Path(__file__).resolve().parents[1] / "Dockerfile").exists(),
                    reason="Dockerfile 尚未落地")
def test_dockerfile_does_not_pip_install_package():
    """★ E4 护栏：不能用 `pip install .`。

    pyproject 的 packages 不含 media_resource_manager.web / bundled_models，
    且无 package-data ⇒ pip 装完镜像里没有前端资产，Web.start() 直接中文报
    「Web 前端资源缺失」。这条断言把 R101 的踩坑结论钉死，防止以后有人"顺手"。
    """
    text = _dockerfile_instructions(_read(REPO_ROOT / "Dockerfile"))
    assert "pip install ." not in text.replace("pip install .[", "pip install x"), (
        "镜像里用 pip install . 会漏掉 web/ 前端资产（E4 必红），请改走 COPY 源码树")
    assert "requirements-docker.txt" in text, "应改用 requirements-docker.txt"


def test_web_assets_exist_in_repo():
    """E4 前置：源码树里前端资产必须在位（COPY 源码树才带得进镜像）。

    ⚠ 真实布局是 web/index.html + web/static/js/app.js + web/static/css/app.css
      + web/sw.js + web/manifest.webmanifest + web/static/img/，
      **不是** 订单正文简写的 web/static/* 那种"三个文件平铺"。
      R102 先照订单写了 web/app.js，实测全部假红，已按真实目录改正。
    """
    web = REPO_ROOT / "media_resource_manager" / "web"
    # 2026-10-29：旧 SPA 三件套已删除，改查新版资产 + 构建产物。
    for rel in ("index.v2.html", "static/js/qr-login.js",
                "sw.js", "manifest.webmanifest"):
        assert (web / rel).exists(), f"前端资产缺失：web/{rel}"
    assert list((web / "static" / "app" / "assets").glob("*.js")), \
        "前端产物缺失：web/static/app/assets/*.js"


def test_webui_static_whitelist_keeps_paths_open():
    """E2/E3 相关：这五个路径必须在鉴权之外（webui.py 的实测结论）。

    容器里如果哪天把它们挪到鉴权后面，登录页自己都刷不出来（CSS/JS 401）。
    这里只钉住"源码里存在这条放行口径"，不发起真实请求。
    """
    src = _read(REPO_ROOT / "media_resource_manager" / "webui.py")
    for rel in ("index.html", "sw.js", "manifest.webmanifest",
                "static/css/app.css", "static/js/app.js"):
        assert rel in src, f"webui.py 里找不到静态放行路径 {rel}"


@pytest.mark.skipif(not (Path(__file__).resolve().parents[1] / "docker-compose.yml").exists(),
                    reason="docker-compose.yml 尚未落地")
def test_compose_shape():
    """D1/D2/D4：卷映射、端口、挂机重启策略。"""
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load(_read(REPO_ROOT / "docker-compose.yml"))
    svc = list(data["services"].values())[0]

    assert svc.get("restart") == "unless-stopped", "D4 挂机场景必须 unless-stopped"
    joined_volumes = " ".join(str(v) for v in svc.get("volumes", []))
    assert "/data" in joined_volumes, "D1 数据卷必须挂到 /data"
    joined_ports = " ".join(str(p) for p in svc.get("ports", []))
    assert "8080" in joined_ports, "D2 端口映射 8080:8080"

    env = svc.get("environment") or {}
    if isinstance(env, dict):
        assert "MRM_HOME" in env, "★ 不显式设 MRM_HOME 数据会落容器可写层，F7 假绿"
        assert str(env.get("MRM_WEB_LAN_OPEN", "1")) in ("0", "False", "false"), (
            "容器内 lan_open 默认 True ⇒ 全网免密（安全级）")
