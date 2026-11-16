"""V0.0.25 护栏：日志「谁操作的」必须分得清客户端与浏览器。

用户原话：
    「现在还有一个问题，谁操作的？在客户端也显示的是网页吗？现在不是已经
      把 web 端网页当做客户端了吗，如果在这里操作，不能显示是在客户端吗」

背景：V0.0.22 及更早，审计写入把 actor 写死成 "web"，日志页统一翻成
「网页」—— 用户在本机客户端里点的每一下都显示成「网页」，等于这一列没有
信息。V0.0.23 起前端按 `window.__MRM_NATIVE_SHELL__` 带 `X-MRM-Surface`
请求头，后端 `_actor()` 据此记 client / browser；V0.0.25 再把历史值 "web"
翻成「旧记录」，不许再显示成「网页」。

钉三件事：
  1. 映射表：client→客户端、browser→浏览器、web 不得是「网页」；
  2. 前端每个请求都要带 X-MRM-Surface（漏一个就会退化成 browser）；
  3. 后端 _actor() 必须真的读这个头。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "media_resource_manager" / "webapp" / "src"

from media_resource_manager.audit_text import ACTOR_CN, actor_label  # noqa: E402


def test_actor_labels_are_unambiguous():
    assert actor_label("client") == "客户端"
    assert actor_label("browser") == "浏览器"
    # 历史值必须是「旧记录」，不能是「网页」——否则用户会以为自己被骗了
    assert actor_label("web") != "网页", "旧记录又被显示成「网页」了"
    assert actor_label("web") == "旧记录"
    # 内置窗口界面（SweetWeb.exe）也是本机软件，归到「客户端」
    assert actor_label("gui") == "客户端"
    # 采集引擎 / AI 这些非人工动作要保留各自的说法
    assert actor_label("engine") == "采集引擎"
    assert actor_label("ai") == "AI 审核"


def test_frontend_sends_surface_header():
    api = (WEB / "api.js").read_text(encoding="utf-8")
    assert "X-MRM-Surface" in api, "前端请求没带来源头，后端只能猜成 browser"
    assert "__MRM_NATIVE_SHELL__" in api, "来源判断没看壳窗口注入的标记"
    assert "'client'" in api and "'browser'" in api

    # 头必须挂在统一的请求封装上，而不是某个具体接口里（漏一个就退化）
    head = api[:api.index("async function req") + 600]
    assert "X-MRM-Surface" in head, "来源头没有挂在统一请求入口上"


def test_backend_reads_surface_header():
    src = (ROOT / "media_resource_manager" / "webui.py").read_text(encoding="utf-8")
    i = src.index("def _actor(self)")
    block = src[i:i + 700]
    assert "X-MRM-Surface" in block, "后端 _actor() 没读来源头"
    assert 'return "client" if' in block and '"browser"' in block


def test_shell_injects_native_marker():
    """壳窗口必须在页面脚本之前注入标记（否则前端一律按浏览器算）。"""
    src = (ROOT / "media_resource_manager" / "gui" / "web_shell.py").read_text(encoding="utf-8")
    i = src.index("__MRM_NATIVE_SHELL__ = true")
    j = src.index("page = _ShellPage(profile, view)")
    assert i < j, "标记注入必须排在创建页面之前，否则当页收不到"
