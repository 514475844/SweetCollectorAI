"""r242 护栏：诊断包必须**真的脱敏**、且**不含图片字节**。

脱敏这一条最危险：写的时候顺手 ``cfg.to_dict()`` 一整份塞进 zip，
测试全绿、界面也能用，直到有一天用户把诊断包发到群里，代理账号和
接口密钥跟着出去了。所以这里不是「断言有个 mask 函数」，而是**造一份带
凭证的配置 → 真打包 → 打开 zip 读内容**，逐项确认打码落地。
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

from media_resource_manager import diagnostics as dg
from media_resource_manager.config import AppConfig
from media_resource_manager.db import Database

ROOT = Path(__file__).resolve().parents[1]


def _env(tmp_path):
    cfg = AppConfig()
    cfg.db_path = tmp_path / "mrm.db"
    cfg.log_dir = tmp_path / "logs"
    cfg.log_dir.mkdir()
    (cfg.log_dir / "app.log").write_text(
        "INFO 启动\nERROR 下载失败 https://site/x\nERROR 下载失败 https://site/x\n"
        "ERROR 代理认证失败 407\n", encoding="utf-8")
    cfg.proxy = "http://alice:s3cret@127.0.0.1:10808"
    cfg.ai_api_key = "sk-live-abcdef"
    cfg.ai_api_url = "https://api.example/v1"
    cfg.web_password = "hunter2"
    db = Database(cfg.db_path)
    db.execute("INSERT INTO tasks(name) VALUES ('t1')")
    return cfg, db


def test_secrets_are_masked_in_bundle(tmp_path):
    cfg, db = _env(tmp_path)
    out = tmp_path / "diag.zip"
    r = dg.build_bundle(out, cfg=cfg, db=db)
    assert r["bytes"] > 0
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        cfg_text = zf.read("config_masked.json").decode("utf-8")

    assert "config_masked.json" in names
    assert "environment.json" in names
    assert dg.README_NAME in names
    assert any(n.startswith("logs/") for n in names)

    # 明文口令/密钥一个都不能出现
    for leak in ("s3cret", "hunter2", "sk-live-abcdef"):
        assert leak not in cfg_text, leak
    data = json.loads(cfg_text)
    # 代理只掩 user:pass，主机端口留着（排障要看这个）
    assert data["proxy"] == "http://***@127.0.0.1:10808"
    assert data["ai_api_key"] == "***"
    db.close()


def test_bundle_has_no_image_bytes(tmp_path):
    """包里只有元数据：不能出现任何图片后缀的成员（缩略图也不行）。"""
    cfg, db = _env(tmp_path)
    (tmp_path / "thumbs").mkdir()
    (tmp_path / "thumbs" / "1_256.jpg").write_bytes(b"\xff\xd8" * 100)
    out = tmp_path / "diag.zip"
    dg.build_bundle(out, cfg=cfg, db=db)
    with zipfile.ZipFile(out) as zf:
        for n in zf.namelist():
            assert not n.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")), n
    db.close()


def test_readme_tells_user_what_is_inside(tmp_path):
    """说明文件必须写明「含来源网址」——用户发出去之前有权知道自己发了什么。"""
    cfg, db = _env(tmp_path)
    out = tmp_path / "diag.zip"
    dg.build_bundle(out, cfg=cfg, db=db)
    with zipfile.ZipFile(out) as zf:
        text = zf.read(dg.README_NAME).decode("utf-8")
    assert "没有" in text and "图片" in text
    assert "来源网址" in text
    db.close()


def test_log_tail_is_bounded(tmp_path):
    cfg, db = _env(tmp_path)
    big = cfg.log_dir / "huge.log"
    big.write_text("x" * (dg.LOG_TAIL * 3), encoding="utf-8")
    files = dg.collect_logs(cfg.log_dir)
    assert files and len(files) <= dg.LOG_FILES
    name, txt = files[0]
    assert name == "huge.log" or name == "app.log"
    for _n, t in files:
        assert len(t) <= dg.LOG_TAIL + 200      # 允许截断提示那一行
    db.close()


def test_error_digest_dedups():
    text = "\n".join(["INFO ok"] + ["ERROR 下载失败 x"] * 5 + ["ERROR 407 代理"])
    out = dg.error_digest([("app.log", text)])
    assert out.count("下载失败") == 1
    assert "407" in out


def test_error_digest_ignores_numbers_in_urls_and_info():
    """真机上 4xx/5xx 数字会出现在**URL、分页参数、时间戳毫秒**里（实测摘要被塞满）。

    真实样本（部署版 data/logs 里抄出来的）：
      ``13:13:45,474``（毫秒 474）、``?page=400``、``pool_id=484``、
      ``/post/show/409407``、``3_429.jpg``。
    裸数字一律不算线索，只有 ``HTTP 502`` / ``status: 403`` / ``状态码 429``
    这种带上下文的才算。
    """
    text = "\n".join([
        "2026-10-26 13:13:45,474 INFO mrm.engine: 发现页面: https://cdn.x/3_429.jpg (图片=0)",
        "2026-10-26 13:14:05,939 INFO mrm.engine: 发现页面: https://konachan.com/post/show/409407 (图片=1)",
        "2026-10-26 13:19:11,518 INFO mrm.engine: 发现页面: https://ososedki.com/?page=400 (图片=25)",
        "2026-10-26 13:14:09,969 INFO mrm.engine: 发现页面: https://yande.re/post/show/65457?pool_id=484 (图片=6)",
        "2026-10-26 13:17:00,000 INFO mrm.repo: 写入 500 条偏好",
        "2026-10-26 13:15:00,000 ERROR mrm.engine: 下载失败 429 Too Many Requests",
        "2026-10-26 13:16:00,000 WARNING mrm.proxy: 代理认证失败 HTTP 407",
        "2026-10-26 13:20:00,000 ERROR mrm.net: 上游返回 status: 502",
    ])
    out = dg.error_digest([("mrm.log", text)])
    assert "3_429.jpg" not in out
    assert "409407" not in out
    assert "page=400" not in out
    assert "pool_id=484" not in out
    assert "写入 500 条" not in out
    assert "Too Many Requests" in out
    assert "HTTP 407" in out
    assert "status: 502" in out


def test_error_digest_on_clean_info_log_is_empty():
    """全是 INFO 的正常日志 → 摘要为空（不能凭空造出「可疑行」）。"""
    text = "\n".join("2026-10-26 13:13:%02d,474 INFO mrm.engine: 第 %d 张入库" % (i, i)
                     for i in range(30))
    assert dg.error_digest([("mrm.log", text)]) == ""


def test_mask_config_keeps_bools_and_empty():
    out = dg.mask_config({"web_password": "", "use_proxy": True,
                          "ai_api_key": None, "log_max_mb": 10})
    assert out["web_password"] == ""       # 没配就是没配，不该显示 ***
    assert out["use_proxy"] is True
    assert out["ai_api_key"] is None
    assert out["log_max_mb"] == 10


def test_route_and_frontend_wired():
    src = (ROOT / "media_resource_manager/webui.py").read_text(encoding="utf-8")
    assert '"/api/diagnostic"' in src
    assert "_api_diagnostic" in src
    # 诊断包必须走 Content-Disposition 下载，而不是当 JSON 返回
    i = src.index("def _api_diagnostic")
    j = src.index("def _api_prefs_export")
    body = src[i:j]
    # V0.0.15 起诊断包按需求 19 加密：网页端从明文 zip 改成与桌面端一致的
    # .mrmx。护栏跟着升级，不能继续把「送出明文 zip」当成正确行为。
    assert "Content-Disposition" in body and ".mrmx" in body
    assert "ek.encrypted(" in body
    assert "application/zip" not in body

    s = (ROOT / "media_resource_manager/webapp/src/views/Settings.vue"
         ).read_text(encoding="utf-8")
    assert "diagnosticUrl" in s
    a = (ROOT / "media_resource_manager/webapp/src/api.js").read_text(encoding="utf-8")
    assert "/api/diagnostic?db=" in a
