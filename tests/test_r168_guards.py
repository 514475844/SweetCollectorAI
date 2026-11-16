"""R168 护栏：在线模型下载（浏览器 UA / auto 链重排 / HF 超时收紧 /
魔搭映射 / 失败中文可操作 / 下载回调 1 参兼容）。

用户真机现象（2026-10-20 11:27 台式机实测）：
  · huggingface.co   → 超时（国内直连不通，proxy=null）
  · hf-mirror.com    → 403（裸 urllib UA 被拒）；带浏览器 UA → 200
  · modelscope.cn    → 200 可达

外加一条 P0 报错原文：
    后台任务结果处理失败: TypeError: ..._start_model_dl.<locals>._run
    .<locals>.<lambda>() takes 0 positional arguments but 1 was given
  ⇒ 0 参 lambda 被 _on_bg_result 按 cb(res) 调 ⇒ TypeError 吞进日志 ⇒
    进度条/完成弹窗/「设为当前」全链断。

本护栏钉「行为」而不是「调了哪个函数」：
  · 真的构造 Request 并断言 UA 落在 headers 上（含 Range 续传那支）；
  · 真的调 mirror_urls 断言 auto 顺序；
  · 真的跑 download_model（monkeypatch 掉网络）断言换源与失败文案；
  · 静态钉 GUI 两处 emit 的 lambda 能收 1 个位置参数。
"""
from __future__ import annotations

import ast
import pathlib
import re
import urllib.request

import pytest

import media_resource_manager.models_lib as M

ROOT = pathlib.Path(__file__).resolve().parents[1]
MW_PATH = ROOT / "media_resource_manager" / "gui" / "main_window.py"


def _mw_src() -> str:
    return MW_PATH.read_text(encoding="utf-8")


# ---------------- ① 浏览器式 UA ----------------------------------------


def test_browser_ua_looks_like_a_browser():
    """UA 必须像浏览器 —— hf-mirror 对「不像浏览器」的 UA 直接 403。"""
    ua = M.UA_BROWSER
    assert "Mozilla/5.0" in ua, "UA 不以 Mozilla 开头 —— hf-mirror 会 403"
    assert "AppleWebKit" in ua and "Safari" in ua, "UA 缺少浏览器特征串"
    # 旧值（被 403 的那个）必须不再出现在协议头里
    assert ua.strip() != "mrm/1.0"


def test_download_one_sends_browser_ua_and_range(monkeypatch, tmp_path):
    """_download_one 的请求头必须带浏览器 UA；续传时还要带 Range。"""
    seen = {}

    class _FakeResp:
        status = 200
        headers = {"Content-Length": "4"}

        def read(self, n=-1):
            if getattr(self, "_done", False):
                return b""
            self._done = True
            return b"data"

        def close(self):
            pass

    def fake_open(req):
        seen["headers"] = dict(req.headers)
        return _FakeResp()

    monkeypatch.setattr(M, "_open_url", fake_open)
    dest = tmp_path / "config.json"
    M._download_one("https://hf-mirror.com/x/config.json", dest, None, None,
                    source="hf-mirror 国内镜像")
    hdrs = {k.lower(): v for k, v in seen["headers"].items()}
    assert "user-agent" in hdrs, "下载请求没带 UA —— hf-mirror 会 403（R168 复发）"
    assert "Mozilla/5.0" in hdrs["user-agent"], "UA 不是浏览器式"
    assert "range" not in hdrs, "首次下载不该带 Range"

    # 续传支：先留一个 .part
    part = dest.with_suffix(dest.suffix + ".part")
    part.write_bytes(b"xx")
    seen.clear()
    M._download_one("https://hf-mirror.com/x/config.json", dest, None, None,
                    source="hf-mirror 国内镜像")
    hdrs2 = {k.lower(): v for k, v in seen["headers"].items()}
    assert "Mozilla/5.0" in hdrs2.get("user-agent", ""), \
        "Range 续传请求也必须带浏览器 UA（R168 明确要求）"
    assert hdrs2.get("range") == "bytes=2-", "续传没带正确的 Range"


def test_open_url_timeout_tightened_for_hf():
    """HF 直连单源超时收紧（15s），其余源保持默认（30s）。"""
    assert M.TIMEOUT_HF < M.TIMEOUT, "HF 超时没有收紧 —— auto 用户会在死源上干等"
    assert M._timeout_for("https://huggingface.co/a/b") == M.TIMEOUT_HF
    assert M._timeout_for("https://hf-mirror.com/a/b") == M.TIMEOUT
    assert M._timeout_for("https://modelscope.cn/a/b") == M.TIMEOUT


def test_open_url_uses_per_source_timeout(monkeypatch):
    """_open_url 真的把分源超时传给了 urlopen（不是只在辅助函数里算）。"""
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["timeout"] = timeout
        return None

    monkeypatch.setattr(M, "_PROXY", None)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    req = urllib.request.Request("https://huggingface.co/a/b")
    M._open_url(req)
    assert captured["timeout"] == M.TIMEOUT_HF, "HF 请求没用收紧后的超时"

    req2 = urllib.request.Request("https://hf-mirror.com/a/b")
    M._open_url(req2)
    assert captured["timeout"] == M.TIMEOUT, "镜像请求被误用了 HF 超时"


# ---------------- ② auto 链重排 ----------------------------------------


def test_auto_order_is_mirror_first():
    """auto 默认顺序 = hf-mirror → HF → 魔搭（R168 重排）。"""
    m = M.CATALOG[0]
    srcs = [M.source_of_url(u) for u in M.mirror_urls(m, "config.json", "auto")]
    assert srcs == ["hf-mirror 国内镜像", "HuggingFace", "魔搭 ModelScope"], \
        f"auto 顺序不对：{srcs}"


def test_mirror_menu_label_matches_order():
    """界面下拉里 auto 的文案必须与实际顺序一致（否则是骗用户）。"""
    label = dict(M.MIRRORS)["auto"]
    assert "hf-mirror" in label and "HF" in label and "魔搭" in label
    assert label.index("hf-mirror") < label.index("HF"), \
        "文案顺序与实际顺序不符（用户会按文案预期）"


def test_single_source_prefer_unchanged():
    """用户手动选了具体源，就只用那个源（不能自作主张换）。"""
    m = M.CATALOG[0]
    assert [M.source_of_url(u) for u in M.mirror_urls(m, "x", "modelscope")] == \
        ["魔搭 ModelScope"]
    assert [M.source_of_url(u) for u in M.mirror_urls(m, "x", "hf-mirror")] == \
        ["hf-mirror 国内镜像"]
    assert [M.source_of_url(u) for u in M.mirror_urls(m, "x", "hf")] == \
        ["HuggingFace"]


# ---------------- ③ 换源与失败文案 -------------------------------------


def test_download_switches_to_second_source_and_says_so(monkeypatch, tmp_path):
    """第一源失败、第二源成功 → 成功，且文案明确「已自动切换到…」。"""
    calls = []

    def fake_dl(url, dest, on_progress, cancel, source=""):
        calls.append(source)
        if source == "hf-mirror 国内镜像":
            raise RuntimeError("来源 hf-mirror 国内镜像 失败（HTTP 403）")
        dest.parent.mkdir(parents=True, exist_ok=True)
        (dest.parent / "config.json").write_text("{}", encoding="utf-8")
        if dest.suffix != ".json":
            dest.write_bytes(b"x" * 2048)

    monkeypatch.setattr(M, "_download_one", fake_dl)
    m = M.CATALOG[0]
    ok, msg = M.download_model(tmp_path, m["id"], mirror="auto")
    assert ok is True, msg
    assert "已自动切换到" in msg and "HuggingFace" in msg, \
        f"换源成功却没告诉用户是哪个源：{msg}"
    assert calls[0] == "hf-mirror 国内镜像", "auto 第一源不是 hf-mirror"


def test_download_all_fail_message_is_chinese_and_actionable(monkeypatch,
                                                             tmp_path):
    """三源全失败 → 中文 + 可操作（明确指向代理设置），不是甩异常原文。"""
    def fake_dl(url, dest, on_progress, cancel, source=""):
        raise RuntimeError("timeout（测试模拟）")

    monkeypatch.setattr(M, "_download_one", fake_dl)
    m = M.CATALOG[0]
    ok, msg = M.download_model(tmp_path, m["id"], mirror="auto")
    assert ok is False
    assert "已试来源" in msg, "失败文案丢了「已试来源」"
    assert "代理" in msg, f"全部源失败时没给出下一步（填代理）：{msg}"
    for s in ("hf-mirror 国内镜像", "HuggingFace", "魔搭 ModelScope"):
        assert s in msg, f"失败文案缺少源名 {s}"


def test_download_failure_hint_mentions_403():
    """403（镜像站拒 UA）时给出针对性提示，而不是笼统的"失败"。"""
    hint = M._fail_hint("config.json", "", ["hf-mirror 国内镜像"],
                        {"hf-mirror 国内镜像": "HTTP 403"})
    assert "403" in hint


def test_download_verify_failure_is_actionable(monkeypatch, tmp_path):
    """下载完但校验不过 → 明确说「再点一次续传 / 填代理」。"""
    def fake_dl(url, dest, on_progress, cancel, source=""):
        dest.parent.mkdir(parents=True, exist_ok=True)
        # 故意写一个太小的 onnx（<1KB）⇒ is_ready 判不过
        if dest.suffix == ".onnx":
            dest.write_bytes(b"x" * 10)
        else:
            (dest.parent / "config.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(M, "_download_one", fake_dl)
    m = M.CATALOG[0]
    ok, msg = M.download_model(tmp_path, m["id"], mirror="auto")
    assert ok is False
    assert "校验" in msg and ("续传" in msg or "代理" in msg), \
        f"校验失败文案不可操作：{msg}"


# ---------------- ④ 魔搭映射完整性 -------------------------------------


def test_every_catalog_model_has_ms_namespace():
    """每个内置模型都要有魔搭命名空间，否则兜底链有个洞。"""
    for m in M.CATALOG:
        assert m.get("ms"), f"{m['id']} 缺 ms 命名空间（魔搭兜底不可用）"
        assert "/" in m["ms"], f"{m['id']} 的 ms 不是 命名空间/模型名 形式"


def test_ms_url_shape_is_file_api():
    """魔搭 URL 必须是它的文件接口（不是换域名的 HF 路径）。"""
    m = M.CATALOG[0]
    u = M._ms_url(m, "onnx/model_quantized.onnx")
    assert "modelscope.cn/api/v1/models/" in u
    assert "FilePath=" in u and "Revision=" in u


# ---------------- ⑤ P0：emit 的 lambda 必须收 1 参 ----------------------
#
# 报错原文：lambda() takes 0 positional arguments but 1 was given
# _on_bg_result 统一 cb(res) 调用 ⇒ 所有 emit 的 cb 必须能吃 1 个位置参数。
# 这里用**真调用**验证：取出 emit 的 cb，按 (res) 调一次，不许 TypeError。


def _collect_emitted_cbs(src: str) -> list[ast.AST]:
    """AST 找所有 bg_result.emit((cb, ...)) 里的第一个元素。"""
    tree = ast.parse(src)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Attribute) and f.attr == "emit"):
            continue
        if not node.args:
            continue
        tup = node.args[0]
        if isinstance(tup, ast.Tuple) and tup.elts:
            out.append(tup.elts[0])
    return out


def test_all_emitted_callbacks_absorb_positional_arg():
    """每个 bg_result.emit 的 cb 都必须用 ``*_a`` **吸收**位置参数。

    ⚠ 判据是「有 vararg」而不是「参数个数够」——因为真正的坑有两层：
      · ``_run`` 的 ``lambda: _fin(...)`` 是 0 参 ⇒ 直接 TypeError（报错原文）；
      · ``_on_prog`` 的 ``lambda d=rel, dn=done, ...`` 有默认值 ⇒ **不报错**，
        但 _on_bg_result 传进来的 res 会顶替掉 ``d``（显示成 None），
        属于「静默错」——比崩溃更难查。
    只有 ``*_a`` 才能同时挡住这两层。
    """
    src = _mw_src()
    cbs = _collect_emitted_cbs(src)
    assert cbs, "没找到任何 bg_result.emit —— 定位逻辑失效"
    bad = []
    for cb in cbs:
        if isinstance(cb, ast.Lambda) and cb.args.vararg is None:
            bad.append(ast.unparse(cb)[:90])
    assert not bad, ("这些 emit 的 lambda 没有用 *_a 吸收位置参数:  _on_bg_result "
                     "按 cb(res) 调用，0 参会 TypeError、带默认值的会静默错位"
                     "（R168 P0 复发）：\n" + "\n".join(bad))


def test_model_dl_lambdas_use_star_args():
    """两处下载回调的 lambda 显式用 *_a 兜住位置参数（回归钉）。"""
    src = _mw_src()
    assert "lambda *_a, d=rel, dn=done" in src, \
        "_on_prog 的 lambda 没改成 1 参兼容（进度显示会错位/不刷新）"
    assert "lambda *_a: _fin(*wk.result)" in src, \
        "_run 的 lambda 没改成 1 参兼容（完成弹窗/设为当前全链断）"
    # 旧的 0 参写法必须消失
    bad = re.search(r"bg_result\.emit\(\(lambda:", src)
    assert bad is None, "还有 0 参 lambda 挂在 bg_result.emit 上"


@pytest.mark.parametrize("old,new", [
    ("lambda *_a, d=rel, dn=done", "lambda d=rel, dn=done"),
    ("lambda *_a: _fin(*wk.result)", "lambda: _fin(*wk.result)"),
])
def test_p0_mutation_is_caught(old, new):
    """反向可变性：两处 lambda 各自退回修改前的写法，检查必须变红。

    在内存里变异，不动磁盘文件（源码逐字节保全）。
    """
    src = _mw_src()
    mutated = src.replace(old, new)
    assert mutated != src, f"变异锚点失效（源码已变）：{old}"
    cbs = _collect_emitted_cbs(mutated)
    bad = [c for c in cbs
           if isinstance(c, ast.Lambda) and c.args.vararg is None]
    assert bad, f"变异成 {new!r} 后竟然判定为合规 —— 护栏没在真防盗"


def test_on_prog_positional_arg_does_not_clobber_filename():
    """行为级：把 _on_prog 的 lambda 真调一次（带位置参数），
    第 1 个（显示用）参数必须仍是 rel，而不是被 res 顶掉。"""
    import types
    src = _mw_src()
    node = None
    for cb in _collect_emitted_cbs(src):
        if isinstance(cb, ast.Lambda) and "d=rel" in ast.unparse(cb):
            node = cb
            break
    assert node is not None, "找不到 _on_prog 的 lambda"
    code = ast.unparse(node)
    lam = eval(code, {  # noqa: S307 - 只 eval 项目自己源码里的这一段 lambda
        "rel": "onnx/model_quantized.onnx",
        "done": 10, "total": 100, "cur": "　来源：X", "tried_txt": "",
        "bar": types.SimpleNamespace(setValue=lambda v: None),
        "status": types.SimpleNamespace(setText=lambda s: None),
        "name": "CLIP",
    })
    # _on_bg_result 会这样调：cb(res)
    lam(None)
    # 没有异常即达标（旧 0 参写法在这里会 TypeError）

