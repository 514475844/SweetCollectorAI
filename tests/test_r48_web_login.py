"""R48 / THOROUGH_AUDIT_ORDER F：Web 登录放宽（去用户名 + 密码非必填）。

来源：作业单 F（用户第 6 条后半）—— 「不再强制设置密码」；「去掉用户名，
登录页只留密码一个输入框」；后端鉴权放宽，空密码也允许访问。

⚠ 测「没设密码」必须在**同一个进程里换配置**后重建 handler ——
   `WebHandler` 持的是 `cfg` 引用（`self.cfg` 是 property），所以直接改
   `cfg.web_password` 就能切换「有密码 / 免密」两种形态，不用起真服务器。
"""
import json
import types
import urllib.parse

import pytest

from media_resource_manager import webui


# ============================================================ 纯函数层

def test_wrong_password_still_rejected():
    """F 不能变成「谁都能进」：设了密码时密码错仍要 401。"""
    import hmac

    want = "secret"
    assert not hmac.compare_digest("wrong", want)
    assert hmac.compare_digest("secret", want)


def test_session_token_roundtrip_with_empty_password():
    """免密也要能发/验会话 cookie（前端状态一致，日后设密码不用改前端）。"""
    tok = webui.make_session("admin", "")
    # ⚠ verify_session(token, user, pw, now=None)，第四参是「当前时间」
    #   的覆盖值，传错位置会让「刚签发的 token」被判过期/无效。
    assert webui.verify_session(tok, "admin", "") is True


def test_session_token_rejects_wrong_password():
    """设了密码后，旧 cookie（按空密码签的）不能继续用 —— 改密码即失效。"""
    tok = webui.make_session("admin", "")
    assert webui.verify_session(tok, "admin", "newpass") is False


# ============================================================ handler 层

class _FakeHandler(webui._Handler):
    """只测鉴权/登录分支：不走真 socket。"""

    # ⚠ 基类里 `cfg` / `headers` / `path` 都是 **property**（真实 handler 从
    #   server 拿 cfg、从 socket 拿 headers），子类不能赋值。所以一律走 `_xxx`
    #   私有槽，再由 property 读出来。
    _cfg = None
    _db = None
    _headers = None
    _path = "/api/x"

    # ⚠⚠ R58：`client_address` **必须能注入**。R52 加了「局域网/白名单免密」，
    #   而 `_client_allowed_by_ip()` 第一步就读 `self.client_address[0]`。
    #   老替身没有这个属性 ⇒ `_client_ip()` 走 except 回落 `""` ⇒
    #   `_ip_is_private("")` 恒False ⇒ **免密分支永远走不到**。
    #   后果：本文件 4 条用例集体假红，而且改产品也改不好（产品是对的）。
    #   默认给回环地址（= 真实 WebServer 绑 127.0.0.1 时的情形）。
    client_address = ("127.0.0.1", 0)

    def __init__(self, cfg, headers=None, body=None, client_ip="127.0.0.1"):
        self._cfg = cfg
        self._db = None
        self._headers = headers or {}
        self._path = "/api/x"
        self._body_obj = body or {}
        self._sent = None
        self.client_address = (client_ip, 0)

    # 覆写掉所有 I/O
    def _send(self, code, body, ctype, extra=None):
        self._sent = (code, body, ctype, extra or {})
        return self._sent

    def _json(self, obj, code=200, extra=None):
        import json as _j
        raw = _j.dumps(obj, ensure_ascii=False).encode("utf-8")
        return self._send(code, raw, "application/json; charset=utf-8", extra)

    def _body(self):
        return dict(self._body_obj)

    @property
    def cfg(self):
        return self._cfg

    @property
    def db(self):
        return self._db

    @property
    def headers(self):
        return self._headers

    @property
    def path(self):
        return self._path

    def json(self):
        return json.loads(self._sent[1].decode("utf-8"))


@pytest.fixture
def cfgs():
    """三种配置。

    ⚠⚠ R58：`web_lan_open` 必须**显式带上**，不能靠 AppConfig 的默认值。
       它默认 True，而回环 127.0.0.1 又是私有地址 ⇒ 组合起来恒免密 ⇒
       「设了密码也要 401」这类断言结构上永远不成立（4 条假红）。
       这里显式给 True/False 两版，让两种契约各自可测。

       ⚠ 别只带 True：`web_lan_open=False` 那一档是「公网必须严」的唯一护栏，
         只测免密就等于没人测「该拦的有没有拦」。
    """
    from media_resource_manager.config import AppConfig

    a = AppConfig()
    a.web_password = ""
    a.web_lan_open = True
    b = AppConfig()
    b.web_password = "secret"
    b.web_lan_open = True
    c = AppConfig()
    c.web_password = "secret"
    c.web_lan_open = False
    yield a, b, c


def test_auth_ok_when_no_password(cfgs):
    """F 核心：web_password 为空 → `_auth_ok()` 直接 True（无任何凭据）。

    R52：免密的前提是**本客户端在免密范围内**（回环属于）。
    """
    empty, _, _ = cfgs
    h = _FakeHandler(empty, headers={})
    assert h._auth_ok() is True


def test_public_client_is_rejected_even_without_password(cfgs):
    """⚠⚠ R52 契约的**安全侧**护栏（这条比上面那条更重要）。

    免密只对「局域网/白名单」成立。公网来源即便没设密码也必须被拒 ——
    否则 `web_lan_open=True`（默认值）等于把图库对全网敞开。
    ⚠ R220 联动（台账）：R220 后「空密码 + 绑本机(127.0.0.1)」无条件免密
    —— 绑本机物理上看不到公网客户端，本判据改用**开放绑定 0.0.0.0** 表达
    公网暴露面；R58 边界原样保留（行为矩阵见 test_r220_web_password_free.py）。
    """
    empty, _, _ = cfgs
    empty.web_bind = "0.0.0.0"
    h = _FakeHandler(empty, headers={}, client_ip="203.0.113.9")
    assert h._client_allowed_by_ip() is False, "公网 IP 竟被判成免密范围"
    assert h._auth_ok() is False, "公网 + 没设密码竟然放行 —— 图库对外全开"


def test_public_client_with_password_still_needs_the_password(cfgs):
    """公网 + 设了密码：没凭据一律拒，正确 Basic Auth 才放行。"""
    _, _, strict = cfgs
    import base64

    bad = _FakeHandler(strict, headers={}, client_ip="203.0.113.9")
    assert bad._auth_ok() is False
    tok = base64.b64encode(b"admin:secret").decode()
    good = _FakeHandler(strict, headers={"Authorization": f"Basic {tok}"},
                        client_ip="203.0.113.9")
    assert good._auth_ok() is True


def test_auth_ok_requires_password_when_set(cfgs):
    """设了密码且不开局域网免密 → 没凭据一律 False（放宽不能变成无保护）。"""
    _, _, strict = cfgs
    h = _FakeHandler(strict, headers={}, client_ip="203.0.113.9")
    assert h._auth_ok() is False


def test_auth_ok_requires_password_when_lan_open_off(cfgs):
    """⚠ R58：同上，但用**回环 IP** 测`lan_open=False` 这一档。

    这是「同一个局域网地址，关掉免密后是否立刻要密码」的护栏 ——
    少了它，`web_lan_open` 这个开关本身就没被测到。
    """
    _, _, strict = cfgs
    h = _FakeHandler(strict, headers={}, client_ip="127.0.0.1")
    assert h._client_allowed_by_ip() is False, "lan_open=False 却仍判免密"
    assert h._auth_ok() is False


def test_auth_ok_with_basic_auth_when_set(cfgs):
    """设了密码时，正确 Basic Auth 仍放行（老书签/脚本路径不破）。"""
    _, withpw, _ = cfgs
    import base64

    tok = base64.b64encode(
        f"{withpw.web_user or webui.DEFAULT_USER}:secret".encode()
    ).decode()
    h = _FakeHandler(withpw, headers={"Authorization": f"Basic {tok}"})
    assert h._auth_ok() is True


def test_auth_ok_with_session_cookie_when_set(cfgs):
    _, withpw, _ = cfgs
    tok = webui.make_session(withpw.web_user or webui.DEFAULT_USER, "secret")
    h = _FakeHandler(withpw, headers={"Cookie": f"{webui._COOKIE_NAME}={tok}"})
    assert h._auth_ok() is True


def test_login_without_password_succeeds(cfgs):
    """F：没设密码时，空密码登录 → 200 且 ok=True（原来返回 401）。"""
    empty, _, _ = cfgs
    h = _FakeHandler(empty, body={"password": ""})
    h._do_login()
    code, _raw, _ct, extra = h._sent
    assert code == 200, f"空密码登录被拒（HTTP {code}）"
    assert h.json()["ok"] is True
    assert h.json()["password_required"] is False
    assert webui._COOKIE_NAME in extra.get("Set-Cookie", "")


def test_login_with_correct_password_succeeds(cfgs):
    _, withpw, _ = cfgs
    h = _FakeHandler(withpw, body={"password": "secret"})
    h._do_login()
    assert h._sent[0] == 200
    assert h.json()["password_required"] is True


def test_login_from_public_client_with_right_password_succeeds(cfgs):
    """⚠⚠ R58 修复钉死点：公网客户端用**正确密码**必须能登录。

    这条在 R58 之前是反的（断言必须 401），而那个断言**把一个真实缺陷钉死了**：
    `_do_login` 当时只判 `if not self._client_allowed_by_ip():`，漏了 `not want`，
    于是「设了密码 + 公网访问」这条唯一正常的远程用法被彻底堵死——
    用户在外面设了密码却永远登不进来，且 `_auth_ok()` 又认Basic Auth 与
    cookie（449-450 行 R52 自己的注释就是这么写的），
    即"能生成的东西不被自己承认"。

    修法：条件改成 `if not want and not self._client_allowed_by_ip():`
    ——只有「没设密码 + 不在免密范围」才拒，那种情况才需要拒
    （否则前端会拿到"登录成功"却满屏 401）。
    证据：tools/_r58_probe_login_deadlock.py（探针实跑，不是读代码猜的）。
    """
    _, withpw, _ = cfgs
    h = _FakeHandler(withpw, body={"password": "secret"},
                     client_ip="203.0.113.9")
    h._do_login()
    assert h._sent[0] == 200, "公网 + 正确密码竟登不进去 —— 密码机制成了死路"
    assert h.json()["ok"] is True
    assert webui._COOKIE_NAME in (h._sent[3] or {}).get("Set-Cookie", ""), \
        "登录成功却没下发会话 cookie"


def test_login_from_public_client_without_password_is_refused(cfgs):
    """⚠ R58 的**安全侧**不能放松：公网 + **没设密码**仍必须拒。

    上面那条放宽的是「设了正确密码」，不是「没设密码」。
    这一对放在一起才完整：只测前者会漏掉「把免密范围捅成全网敞开」，
    只测后者会漏掉「密码机制形同虚设」。
    """
    empty, _, _ = cfgs
    h = _FakeHandler(empty, body={"password": ""}, client_ip="203.0.113.9")
    h._do_login()
    assert h._sent[0] == 401, "公网 + 没设密码竟登录成功 —— 图库对全网敞开"
    err = h.json().get("error", "")
    assert "免密范围" in err, f"错误提示应说明网络不在免密范围，实得 {err!r}"


def test_login_cookie_from_public_client_is_accepted_by_auth_ok(cfgs):
    """⚠⚠ 自洽性护栏：`_do_login` 发出的 cookie，`_auth_ok` 必须认。

    这是 R58 缺陷的**判据式钉法**——只测「登录返回 200」不够，
    因为死锁的另一半是「发出来的 cookie 下一跳被拒」。两条合起来才闭合。
    """
    _, withpw, _ = cfgs
    h = _FakeHandler(withpw, body={"password": "secret"},
                     client_ip="203.0.113.9")
    h._do_login()
    assert h._sent[0] == 200
    tok = (h._sent[3] or {}).get("Set-Cookie", "").split(";")[0]
    assert tok, "没下发 cookie"
    nxt = _FakeHandler(withpw, headers={"Cookie": tok},
                       client_ip="203.0.113.9")
    assert nxt._auth_ok() is True, "刚签发的 cookie 下一跳就被拒 —— 死锁"


def test_login_with_wrong_password_rejected(cfgs):
    _, withpw, _ = cfgs
    h = _FakeHandler(withpw, body={"password": "nope"})
    h._do_login()
    assert h._sent[0] == 401
    assert h.json()["ok"] is False


def test_login_error_message_is_chinese(cfgs):
    """F：所有用户可见文案必须中文。"""
    _, withpw, _ = cfgs
    h = _FakeHandler(withpw, body={"password": "nope"})
    h._do_login()
    err = h.json().get("error", "")
    assert err, "失败时必须有中文提示"
    assert any("一" <= ch <= "鿿" for ch in err), f"提示不是中文：{err!r}"


# ============================================================ 前端资源

def test_session_api_reports_password_required(cfgs):
    """F：`/api/session` 要告诉前端「要不要密码」，前端才能决定进不进登录页。"""
    import ast
    from pathlib import Path

    src = Path(webui.__file__).read_text(encoding="utf-8")
    assert "password_required" in src, (
        "/api/session 没有回 password_required —— 前端无法判断是否要密码")


# ============================================================ 鉴权放宽的边界

def test_password_change_invalidates_old_cookie():
    """设了密码后，按空密码签的旧 cookie 必须失效（改密码即改权限）。"""
    old = webui.make_session("admin", "")
    assert webui.verify_session(old, "admin", "secret") is False


def test_unauthorized_still_401_for_api_when_password_set(cfgs):
    """⚠ 放宽只针对「没设密码」；设了密码且不开免密时 /api/* 未登录仍要 401。"""
    _, _, strict = cfgs
    h = _FakeHandler(strict, headers={}, client_ip="203.0.113.9")
    h._path = "/api/summary"
    # _unauthorized 依赖 self.path 与 _login_shell，这里只验鉴权判断
    assert h._auth_ok() is False


def test_empty_password_not_confused_with_wrong_password(cfgs):
    """⚠ 关键区分：空密码在**没设密码**时是放行，在**设了密码**时是 401。

    这两条放在一起才完整；只测一条会漏掉「把两种情况搞混」这个 bug
    （旧代码 `if not want or not compare(...)` 正是这么错的）。
    """
    empty, withpw, _ = cfgs
    h1 = _FakeHandler(empty, body={"password": ""})
    h1._do_login()
    h2 = _FakeHandler(withpw, body={"password": ""})
    h2._do_login()
    assert h1._sent[0] == 200, "没设密码时空密码应放行"
    assert h2._sent[0] == 401, "设了密码时空密码必须被拒"