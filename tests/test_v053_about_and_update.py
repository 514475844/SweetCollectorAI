"""V0.0.53 护栏：关于页补全 + 检查更新接入 GitHub Release。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "media_resource_manager" / "webapp" / "src"


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


# ── 检查更新：默认 GitHub Release ───────────────────────────
def test_update_defaults_to_github_release():
    s = _read("media_resource_manager/update_check.py")
    assert 'GITHUB_OWNER = "514475844"' in s
    assert 'GITHUB_REPO = "SweetCollectorAI"' in s
    assert "DEFAULT_RELEASE_API" in s and "/releases/latest" in s
    # 没配清单地址时走默认，而不是报"未配置"
    assert "url = custom or DEFAULT_RELEASE_API" in s


def test_parse_github_release_fields():
    import sys
    sys.path.insert(0, str(ROOT))
    from media_resource_manager.update_check import parse_manifest
    data = {
        "tag_name": "v0.0.52",
        "html_url": "https://github.com/x/y/releases/tag/v0.0.52",
        "body": "修复若干问题",
        "published_at": "2026-11-16T14:26:05Z",
    }
    out = parse_manifest(data, True)
    assert out["version"] == "0.0.52"          # 去掉 v 前缀
    assert out["url"].endswith("v0.0.52")
    assert out["notes"] == "修复若干问题"
    assert out["published"] == "2026-11-16"
    # 自定义清单字段不受影响
    custom = parse_manifest({"version": "1.2.3", "url": "u", "notes": "n"}, False)
    assert custom["version"] == "1.2.3" and custom["url"] == "u"


def test_update_failure_not_fake_latest():
    s = _read("media_resource_manager/update_check.py")
    assert "不假装" in s or "不假装" in s
    # 失败分支给出 Releases 页手动查看的入口
    assert "RELEASES_PAGE" in s
    assert "手动查看" in s


def test_github_user_agent_and_custom_first():
    s = _read("media_resource_manager/update_check.py")
    assert "User-Agent" in s and "application/vnd.github+json" in s
    # 用户填了自定义地址就以它为准
    assert "is_release = _is_github_release(url) and not custom" in s


# ── 关于页 ─────────────────────────────────────────────────
def test_about_page_has_intro_and_links():
    s = (WEB / "views/Settings.vue").read_text(encoding="utf-8")
    about = s.split('<section class="card sec" v-show="onTab(\'about\')">', 1)[1]
    about = about.split("</section>", 1)[0]
    for kw in ("Sweet 采集 AI 版", "本地优先", "MIT", "版本 {{ sys.version",
               "项目主页", "更新公告", "问题反馈", "检查程序更新"):
        assert kw in about, kw


def test_about_icons_exist():
    icons = _read("media_resource_manager/webapp/src/icons.js")
    s = (WEB / "views/Settings.vue").read_text(encoding="utf-8")
    about = s.split('<section class="card sec" v-show="onTab(\'about\')">', 1)[1]
    about = about.split("</section>", 1)[0]
    import re
    for name in re.findall(r'<Icon[^>]*name="([\w-]+)"', about):
        assert f'"{name}"' in icons or f"'{name}'" in icons, f"图标缺失: {name}"


def test_version_bumped():
    import re
    m = re.search(r'VERSION = "([\d.]+)"', _read("media_resource_manager/__init__.py"))
    assert m and tuple(int(x) for x in m.group(1).split(".")) >= (0, 0, 53)
