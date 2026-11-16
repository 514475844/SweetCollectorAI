"""V0.0.49 护栏：FTP / SFTP / SMB 网络协议导入。

用户：「导入里我说一个 WebDAV，你就只加一个 WebDAV？还有好多其他协议，
SMB 什么的，包括别的方式，都得考虑到，都得加上。」

落地：新模块 remote_import.py 统一「枚举 → 下载 → 交本地导入链」，
支持 ftp / ftps / sftp / smb；HTTP 作业接口 + 前端第三张来源卡。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MOD = ROOT / "media_resource_manager" / "remote_import.py"
UI = ROOT / "media_resource_manager/webapp/src"
WEBUI = ROOT / "media_resource_manager/webui.py"


def _load_module():
    import sys
    sys.path.insert(0, str(ROOT))
    from media_resource_manager import remote_import
    return remote_import


# ─────────────── 后端模块 ───────────────
def test_supported_protocols():
    m = _load_module()
    assert set(m.DEFAULT_PORT) >= {"ftp", "ftps", "sftp", "smb"}
    assert m.DEFAULT_PORT["sftp"] == 22 and m.DEFAULT_PORT["smb"] == 445


def test_slug_is_stable_and_safe():
    m = _load_module()
    a = m._slug("smb", "192.168.1.100", "/photo/2026")
    b = m._slug("smb", "192.168.1.100", "/photo/2026")
    assert a == b
    assert "smb_" in a and len(a) < 80


def test_unknown_protocol_rejected():
    import types
    m = _load_module()
    cfg = types.SimpleNamespace(library_dir=".")
    out = m.import_remote(None, cfg, "nfs", "host")
    assert out["ok"] is False and "不支持" in out["error"]


def test_missing_host_rejected():
    import types
    m = _load_module()
    cfg = types.SimpleNamespace(library_dir=".")
    out = m.import_remote(None, cfg, "sftp", "")
    assert out["ok"] is False and "服务器地址" in out["error"]


def test_safe_rel_blocks_traversal():
    m = _load_module()
    assert m._safe_rel("/base", "/base/a/b.jpg") == "a/b.jpg"
    # 目录穿越要被削成纯文件名，不许 ../ 写进图库目录
    out = m._safe_rel("/base", "/elsewhere/x.jpg")
    assert ".." not in out and out == "x.jpg"


def test_downloaders_use_expected_libs():
    s = MOD.read_text(encoding="utf-8")
    assert "import ftplib" in s, "FTP 没用标准库"
    assert "paramiko" in s, "SFTP 没用 paramiko"
    assert "smbclient" in s, "SMB 没用 smbclient"


def test_cache_dir_separates_protocols():
    from pathlib import Path
    m = _load_module()
    lib = Path("/tmp/lib")
    assert m.cache_dir_for(lib, "sftp", "h", "/p") != m.cache_dir_for(lib, "smb", "h", "/p")


# ─────────────── HTTP 接口 ───────────────
def test_remote_route_registered():
    s = WEBUI.read_text(encoding="utf-8")
    assert 'path == "/api/import/remote"' in s
    assert "def _api_import_remote" in s


# ─────────────── 前端 ───────────────
def test_three_source_cards():
    s = (UI / "components/ImportFolder.vue").read_text(encoding="utf-8")
    for name in ("本地文件夹", "WebDAV 地址", "FTP / SFTP / SMB"):
        assert name in s, f"来源卡缺 {name}"


def test_protocol_selector_has_all():
    s = (UI / "components/ImportFolder.vue").read_text(encoding="utf-8")
    for v in ("sftp", "smb", "ftp", "ftps"):
        assert f'value="{v}"' in s, f"协议下拉缺 {v}"


def test_import_job_routes_remote():
    s = (UI / "importJob.js").read_text(encoding="utf-8")
    assert "imp.mode === 'remote'" in s
    assert "api.importRemote" in s
    assert "protocol: imp.protocol" in s


def test_api_method_exists():
    s = (UI / "api.js").read_text(encoding="utf-8")
    assert "importRemote" in s and "/api/import/remote" in s


def test_packaging_includes_deps():
    s = (ROOT / "build/pyinstaller.spec").read_text(encoding="utf-8")
    for dep in ("paramiko", "smbprotocol", "smbclient"):
        assert dep in s, f"打包 spec 漏了 {dep}"
