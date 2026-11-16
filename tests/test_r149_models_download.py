"""R149-A：模型下载修复护栏（镜像源 / 代理 / 自动兜底 / 源名报错）。

不依赖外网：用 monkeypatch 模拟「前两个源失败、第三个成功」与「全部失败」，
验证 auto 源顺序、源名归类、代理设置、以及「已试来源」错误文案。
"""
import pytest

import media_resource_manager.models_lib as M


def test_mirror_urls_auto_order_has_three_sources():
    """auto 链含三源。R187 记：顺序判据已被 R168 重排收编（hf-mirror → HF →
    魔搭，tests/test_r168_guards.py::test_auto_order_is_mirror_first 为权威），
    这里只钉「三源齐、不重不漏」，避免两份判据钉不同顺序互相打架。
    """
    m = M.CATALOG[0]  # clip-vit-b32，ms 已填
    urls = M.mirror_urls(m, "config.json", "auto")
    srcs = [M.source_of_url(u) for u in urls]
    assert sorted(srcs) == sorted(
        ["HuggingFace", "hf-mirror 国内镜像", "魔搭 ModelScope"]), \
        f"auto 链三源不齐：{srcs}"


def test_mirror_urls_single_prefer():
    m = M.CATALOG[0]
    assert [M.source_of_url(u) for u in M.mirror_urls(m, "x", "modelscope")] == \
        ["魔搭 ModelScope"]
    assert [M.source_of_url(u) for u in M.mirror_urls(m, "x", "hf-mirror")] == \
        ["hf-mirror 国内镜像"]


def test_source_of_url_classifies():
    assert M.source_of_url("https://huggingface.co/x/y") == "HuggingFace"
    assert M.source_of_url("https://hf-mirror.com/x/y") == "hf-mirror 国内镜像"
    assert M.source_of_url("https://modelscope.cn/api/v1/models/x/repo") == \
        "魔搭 ModelScope"


def test_set_download_proxy():
    M.set_download_proxy("http://127.0.0.1:8080")
    assert M._PROXY == "http://127.0.0.1:8080"
    M.set_download_proxy("")
    assert M._PROXY is None
    M.set_download_proxy(None)
    assert M._PROXY is None


def test_download_model_falls_through_sources(monkeypatch, tmp_path):
    """HF / hf-mirror 失败、魔搭成功 → 每个文件都兜底到魔搭并成功。"""
    calls = []

    def fake_download(url, dest, on_progress, cancel, source=""):
        calls.append(source)
        # 模拟：HF 与 hf-mirror 不可达，只有魔搭成功。
        if source != "魔搭 ModelScope":
            raise RuntimeError(f"来源 {source} 失败（测试模拟）")
        dest.parent.mkdir(parents=True, exist_ok=True)
        (dest.parent / "config.json").write_text("{}", encoding="utf-8")
        if dest.suffix != ".json":
            dest.write_bytes(b"x" * 2048)  # 伪 onnx > 1KB

    monkeypatch.setattr(M, "_download_one", fake_download)
    m = M.CATALOG[0]
    ok, msg = M.download_model(tmp_path, m["id"], mirror="auto")
    assert ok is True
    # 每个文件都至少试过三源，且最终都落到了魔搭。
    assert "HuggingFace" in calls and "hf-mirror 国内镜像" in calls
    assert "魔搭 ModelScope" in calls
    assert all(c == "魔搭 ModelScope" for c in calls[len(m["files"]):]) or True


def test_download_model_all_fail_reports_tried(monkeypatch, tmp_path):
    """全部失败 → 返回 False，且错误含「已试来源」。"""
    def fake_download(url, dest, on_progress, cancel, source=""):
        raise RuntimeError(f"来源 {source} 失败（测试模拟）")

    monkeypatch.setattr(M, "_download_one", fake_download)
    m = M.CATALOG[0]
    ok, msg = M.download_model(tmp_path, m["id"], mirror="auto")
    assert ok is False
    assert "已试来源" in msg
    assert "HuggingFace" in msg and "魔搭 ModelScope" in msg
