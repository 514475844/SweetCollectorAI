"""V0.0.47 护栏：Web 访问设置说人话 + 去掉假设置。

用户反馈：「既然现在已经是 Web 壳了，设置里『开启 Web 访问』是不是多余了？
应该改成『是否允许局域网访问』；相关设置还有哪些要改。」

结论落地：
  1) 界面本来就没有「开启 Web 访问」开关（Web壳必须常开服务），把技术化的
     「监听地址」配一个人话开关「允许局域网访问」（0.0.0.0 ↔ 127.0.0.1），
     原来的手填框降级成「高级」保留，多网卡用户仍能绑具体 IP。
  2) 「登录账号」是假设置 —— R105 起鉴权只认密码、不读 cfg.web_user，
     界面上的输入框改了没任何作用，删掉。
  3) HTTPS 是本地网页的加密入口，从「网络与代理」挪到「Web 访问」分页。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _web(rel: str) -> str:
    return (ROOT / "media_resource_manager" / "webapp" / "src" / rel).read_text(
        encoding="utf-8")


# ─────────────── ① 人话开关 ───────────────
def test_lan_access_toggle_exists():
    s = _web("views/Settings.vue")
    assert "允许局域网访问" in s, "缺「允许局域网访问」人话开关"
    assert "lanOpen" in s, "缺开关绑定值"
    # 勾选写 0.0.0.0、取消写 127.0.0.1
    assert "'0.0.0.0' : '127.0.0.1'" in s, "开关没接到 web_bind"
    assert "允许局域网访问" in s.split("<template>", 1)[1], "开关没在界面上"


def test_raw_bind_kept_as_advanced():
    s = _web("views/Settings.vue")
    assert "监听地址（高级）" in s, "手填框被删了 —— 多网卡用户没法绑具体 IP"
    # 高级框仍写 web_bind
    assert "save({ web_bind: $event.target.value })" in s


def test_lan_open_computed_handles_specific_ip():
    s = _web("views/Settings.vue")
    seg = s.split("const lanOpen = computed", 1)[1].split("})", 1)[0]
    assert "!== '127.0.0.1'" in seg, "具体 IP 应判定为已开放局域网"


# ─────────────── ② 假设置清理 ───────────────
def test_fake_login_account_removed():
    s = _web("views/Settings.vue")
    tpl = s.split("<template>", 1)[1].split("</template>", 1)[0]
    assert "登录账号" not in tpl, "「登录账号」假设置还在（R105 鉴权只认密码）"
    assert "web_user" not in tpl, "web_user 输入框还在界面上"
    assert "仅用于显示，鉴权仅校验密码" not in tpl


def test_no_dead_web_start_switch_claim():
    s = _web("views/Settings.vue")
    assert "无需单独开启" in s, "没说清界面本身就是本地网页、不用开启 Web"


# ─────────────── ③ HTTPS 归位 ───────────────
def test_https_grouped_with_web_access():
    s = _web("views/Settings.vue")
    assert "'HTTPS 加密访问': 'web'" in s, "HTTPS 还归在网络与代理页"
    assert "'HTTPS 加密访问': 'net'" not in s.split("SEC_TAB", 1)[1].split("}", 1)[0]


# ─────────────── ④ 后端不引入死开关 ───────────────
def test_web_shell_still_ignores_web_enabled():
    """Web 壳必须无条件起服务 —— 界面没了才不会白屏。这条不许被改回去。"""
    s = (ROOT / "media_resource_manager" / "gui" / "web_shell.py").read_text(
        encoding="utf-8")
    assert "web_enabled" not in s, "Web 壳又在看 web_enabled 开关了"
    assert "server.start()" in s
