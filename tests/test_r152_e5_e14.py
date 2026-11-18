"""R152-E5 / R152-E14 护栏：局域网免密默认翻转 + headless --config 接线。

E5（安全默认）：web_lan_open 默认 True → False。
  · 默认判据：全新 AppConfig() 的 web_lan_open 必须是 False；
  · 存量兼容：config.json 里存过 True 的，from_dict/load_config 必须原样读回
    （翻默认值 ≠ 偷改存量配置）；
  · 行为层：lan_open=False 时私有地址也要密码；白名单与 lan_open 独立生效；
    docker0 网关 172.17.0.1 是私有段（容器对端恒是它 ⇒ 默认开=全网免密）。
  · 文档层：docker/README.md 与 compose 注释必须与新默认一致（不漂移）。

E14（死参数接线）：headless --config 原来定义了不接。
  · 源码层：headless_main 块内 load_config 必须吃到 args.config（按块切判，
    全文搜会被 build_parser 的 default=None 骗到，R84 同型）；
  · 行为层：build_parser 解析 --config；load_config(显式路径) 真读该文件。

反向可变：默认翻回 True → 第 1 条红；白名单分支删掉 → 第 6 条红；
headless 里 load_config() 不再传 args.config → 源码判据红；文档改回
「默认 True」→ 文档判据红。
"""
from __future__ import annotations

import inspect
import json
import types
from pathlib import Path

from media_resource_manager import headless, webui
from media_resource_manager.config import AppConfig, load_config

ROOT = Path(__file__).resolve().parents[1]


def _code_only(src: str) -> str:
    """剥掉 `#` 注释 —— 判据不许被注释里的字面量骗到（R63-D3）。"""
    return "\n".join(ln.split("#", 1)[0] for ln in src.splitlines())


class _H(webui._Handler):
    """只测 IP 放行分支，不走真 socket（与 R157 同思路）。"""

    _cfg = None
    _db = None
    _headers = {}
    _path = "/api/x"

    def __init__(self, cfg, client_ip="127.0.0.1"):
        self._cfg = cfg
        self._db = None
        self._headers = {}
        self._path = "/api/x"
        self.client_address = (client_ip, 0)

    def _send(self, code, body, ctype, extra=None):
        return (code, body, ctype, extra or {})

    @property
    def cfg(self):
        return self._cfg

    @property
    def headers(self):
        return self._headers

    @property
    def path(self):
        return self._path


def _cfg(lan_open, whitelist=None):
    return types.SimpleNamespace(
        web_password="", web_lan_open=lan_open,
        web_ip_whitelist=whitelist or [], web_bind="127.0.0.1",
        web_port=0, web_enabled=True)


# ---- E5：默认值 ----

def test_e5_default_lan_open_is_false():
    """★ 主判据：全新配置的局域网免密默认必须关（R152-E5 安全默认翻转）。"""
    assert AppConfig().web_lan_open is False, (
        "web_lan_open 默认又被翻回 True —— 同网段免密敞开（容器里=全网免密）")


def test_e5_stored_true_still_loads():
    """存量兼容：config.json 存过 True 的不受影响（翻默认 ≠ 偷改存量）。"""
    cfg = AppConfig.from_dict({"web_lan_open": True})
    assert cfg.web_lan_open is True


def test_e5_roundtrip_keeps_value():
    for val in (True, False):
        cfg = AppConfig.from_dict({"web_lan_open": val})
        assert AppConfig.from_dict(cfg.to_dict()).web_lan_open is val


def test_e5_lan_open_false_private_ip_needs_password():
    """默认档（False）下，局域网地址也必须走密码 —— 免密不再默认敞开。"""
    h = _H(_cfg(False), client_ip="192.168.1.9")
    assert h._client_allowed_by_ip() is False


def test_e5_lan_open_true_private_ip_still_passes():
    """显式打开时行为不变（R52 契约：开了就免密）。"""
    h = _H(_cfg(True), client_ip="192.168.1.9")
    assert h._client_allowed_by_ip() is True


def test_e5_whitelist_independent_of_lan_open():
    """白名单是独立旋钮：lan_open=False 时白名单照样放行（叠加而非互斥）。"""
    h = _H(_cfg(False, whitelist=["192.168.1.9"]), client_ip="192.168.1.9")
    assert h._client_allowed_by_ip() is True


def test_e5_docker0_gateway_is_private():
    """容器对端恒为 docker0 网关（172.17.0.1，私有段）⇒ 默认开=全网免密的
    直接证据；默认档下它不放行（这正是 E5 要修的场景）。"""
    assert webui._Handler._ip_is_private("172.17.0.1") is True
    h = _H(_cfg(False), client_ip="172.17.0.1")
    assert h._client_allowed_by_ip() is False


def test_e5_docs_say_default_false():
    """文档不漂移：docker/README.md 与 compose 注释必须说新默认（R152-E5）。"""
    readme = (ROOT / "docker" / "README.md").read_text(encoding="utf-8")
    assert "R152-E5 安全默认翻转" in readme, (
        "docker/README.md 还在说旧默认（默认 True）—— 文档与代码漂移")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "程序默认 False（R152-E5）" in compose


# ---- E14：--config 接线 ----

def test_e14_parser_accepts_config_path():
    p = Path("/tmp/whatever/config.json")
    args = headless.build_parser().parse_args(["--config", str(p)])
    assert args.config == str(p)


def test_e14_load_config_reads_explicit_path(tmp_path):
    """行为层：load_config(显式路径) 真读该文件（--config 生效的函数本体）。"""
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"web_lan_open": True, "web_port": 12345}),
                        encoding="utf-8")
    cfg = load_config(cfg_file)
    assert cfg.web_lan_open is True
    assert cfg.web_port == 12345


def test_e14_headless_main_wires_config_flag():
    """源码层：headless_main 块内 load_config 必须吃到 args.config。

    按块切判（R84）：全文搜 'args.config' 会被 build_parser 的 default=None
    骗到 ⇒ 只认 headless_main 函数体里的 load_config(...) 调用形态。
    """
    src = _code_only(inspect.getsource(headless.headless_main))
    assert "load_config(Path(args.config) if args.config else None)" in src, (
        "headless_main 里 load_config 没接 --config —— 死参数又回来了")
