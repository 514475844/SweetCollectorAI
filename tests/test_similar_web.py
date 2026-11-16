"""#17 同站查找：CDN 域自学习（离线 fixture，不联网）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from media_resource_manager.gui.similar_web import learn_cdn_hosts  # noqa: E402

# 模拟 4khd 结构：正文图全在 i0.wp.com Photon CDN，本站 host 只有 UI 小图；
# 另有一个只出现 1 张图的广告外域（应被次数门槛挡掉）。
ALBUM_HTML = """
<html><body>
  <img src="/logo.png">
  <img src="https://i0.wp.com/pic.example.com/albums/x/001.jpg?w=1300">
  <img src="https://i0.wp.com/pic.example.com/albums/x/002.jpg?w=1300">
  <img src="https://i0.wp.com/pic.example.com/albums/x/003.jpg?w=1300">
  <img src="https://ads.example.net/banner.gif">
</body></html>
"""


def test_learn_cdn_hosts_picks_repeated_cdn():
    hosts = learn_cdn_hosts(ALBUM_HTML, "https://www.example.com/a/x.html")
    assert "i0.wp.com" in hosts
    assert "ads.example.net" not in hosts, "单张广告图不应被当 CDN"
    assert "www.example.com" not in hosts, "本站 host 不在学习结果里"


def test_learn_cdn_hosts_empty_and_none():
    assert learn_cdn_hosts("", "https://www.example.com/") == set()
    assert learn_cdn_hosts("<html></html>", "https://www.example.com/") == set()


def test_learn_cdn_hosts_min_count():
    assert learn_cdn_hosts(ALBUM_HTML, "https://www.example.com/a/x.html",
                           min_count=4) == set()
