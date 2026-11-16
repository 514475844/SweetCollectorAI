"""「在网站中查找相似」对话框：结果分两段。

上段【同站】：从当前图的来源页出发，抓 来源页 + 上一层目录页 + 页内同站链接
（限页数），抽出同站图片 URL 并下载缩略图展示；用户勾选后保存到
**当前图所在文件夹**（保存位置不变），同名按浏览器规则加 (1)(2)。
下段【全网反查】：一键跳 Google Lens / Bing 视觉搜索 / Yandex / SauceNAO。

全部走任务代理（ProxyPool.resolve），失败时逐页跳过并在日志区提示。
"""
from __future__ import annotations

import html.parser
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from PySide6.QtCore import Qt, QSize, QThread, Signal, QUrl
from PySide6.QtGui import QPixmap, QDesktopServices
from .theme import style as _tstyle   # P1-4：行内样式也要跟随皮肤
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QPushButton,
                               QGroupBox)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp")


class _Parser(html.parser.HTMLParser):
    """收集 <a href> 与 <img src/data-src/data-lazy-src>。"""

    def __init__(self):
        super().__init__()
        self.links: list[str] = []
        self.imgs: list[str] = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "a" and d.get("href"):
            self.links.append(d["href"])
        if tag == "img":
            for k in ("src", "data-src", "data-lazy-src", "data-original"):
                if d.get(k):
                    self.imgs.append(d[k])
                    break


def _fetch(url: str, proxy_url: str | None, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    if proxy_url:
        handler = urllib.request.ProxyHandler(
            {"http": proxy_url, "https": proxy_url})
        opener = urllib.request.build_opener(handler)
    else:
        opener = urllib.request.build_opener()
    with opener.open(req, timeout=timeout) as resp:
        return resp.read()


def _host(url: str) -> str:
    try:
        return urllib.parse.urlsplit(url).netloc.lower()
    except Exception:
        return ""


def learn_cdn_hosts(source_html: str, source_url: str,
                    min_count: int = 2) -> set[str]:
    """#17 从来源页 <img> 的外域 host 学习该站的图片 CDN 域。

    内容图全走 CDN 的站（如 4khd → i0.wp.com Photon），同站段若只按来源页
    host 过滤会一无所获。来源页里同一外域 host 的 <img> 出现 ≥ min_count 次
    即视为该站 CDN 域（广告域通常只有零星一两张，会被次数门槛挡掉）。
    """
    pa = _Parser()
    pa.feed(source_html or "")
    host = _host(source_url)
    cnt: dict[str, int] = {}
    for u in pa.imgs:
        h = _host(urllib.parse.urljoin(source_url, u.split("#")[0]))
        if h and h != host:
            cnt[h] = cnt.get(h, 0) + 1
    return {h for h, c in cnt.items() if c >= min_count}


class SiteSimilarWorker(QThread):
    """抓来源页 + 上层页 + 页内同站链接 → 同站图片 URL → 下载缩略图。"""

    urls_ready = Signal(list)
    thumb = Signal(int, bytes)
    status = Signal(str)
    failed = Signal(str)

    def __init__(self, url: str, proxy_url: str | None,
                 max_pages: int = 10, max_thumbs: int = 60):
        super().__init__()
        self.url = url
        self.proxy_url = proxy_url
        self.max_pages = max_pages
        self.max_thumbs = max_thumbs

    def run(self):  # noqa: C901
        try:
            self.status.emit(f"抓取来源页 {self.url} …")
            base = _fetch(self.url, self.proxy_url).decode("utf-8", "ignore")
            pa = _Parser()
            pa.feed(base)
            host = _host(self.url)
            # #17 同站 CDN 自学习：来源页里 <img> 的外域 host（如 4khd 的
            # i0.wp.com Photon CDN）集中出现即视为该站的图片 CDN 域，
            # 同站段一并放行——否则内容图全走 CDN 的站（4khd 等）会一无所获。
            cdn_hosts = learn_cdn_hosts(base, self.url)
            if cdn_hosts:
                self.status.emit(
                    f"已识别同站 CDN 域：{', '.join(sorted(cdn_hosts))}")
            # 上层目录页：去掉最后一段路径
            parts = urllib.parse.urlsplit(self.url)
            parent = (urllib.parse.urlunsplit(
                (parts.scheme, parts.netloc, parts.path.rsplit("/", 1)[0] + "/",
                 "", "")) if "/" in parts.path.rstrip("/") else "")
            pages = [self.url]
            if parent and _host(parent) == host and parent != self.url:
                pages.append(parent)
            for lk in pa.links:
                if len(pages) >= self.max_pages:
                    break
                if lk.startswith("#") or lk.startswith(("javascript:", "mailto:")):
                    continue
                absu = urllib.parse.urljoin(self.url, lk.split("#")[0])
                if _host(absu) == host and absu not in pages:
                    pages.append(absu)
            imgs: list[str] = []
            for i, pg in enumerate(pages):
                if self.isInterruptionRequested():
                    return
                self.status.emit(f"抓取页面 {i + 1}/{len(pages)}：{pg}")
                try:
                    html_txt = _fetch(pg, self.proxy_url).decode("utf-8", "ignore")
                except Exception as e:
                    self.status.emit(f"  跳过（{type(e).__name__}: {e}）")
                    continue
                pp = _Parser()
                pp.feed(html_txt)
                for u in pp.imgs:
                    absu = urllib.parse.urljoin(pg, u.split("#")[0])
                    if _host(absu) != host and _host(absu) not in cdn_hosts:
                        continue
                    low = absu.lower().split("?")[0]
                    if low.endswith((".svg", ".ico", ".css", ".js")):
                        continue
                    if absu not in imgs:
                        imgs.append(absu)
            self.urls_ready.emit(imgs)
            self.status.emit(f"同站共找到 {len(imgs)} 张图片，正在取缩略图…")
            for i, u in enumerate(imgs[:self.max_thumbs]):
                if self.isInterruptionRequested():
                    return
                try:
                    data = _fetch(u, self.proxy_url, timeout=25)
                    if len(data) <= 8 * 1024 * 1024:
                        self.thumb.emit(i, data)
                except Exception:
                    continue
            self.status.emit("缩略图加载完成")
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"{type(e).__name__}: {e}")


class SaveSelectedWorker(QThread):
    """把勾选的图片下载到目标文件夹（同名加 (1)(2)）。"""

    done = Signal(int, str)      # 成功数, 目标目录
    status = Signal(str)

    def __init__(self, urls: list[str], dest_dir: Path, proxy_url: str | None):
        super().__init__()
        self.urls = urls
        self.dest_dir = Path(dest_dir)
        self.proxy_url = proxy_url

    def run(self):
        self.dest_dir.mkdir(parents=True, exist_ok=True)
        n = 0
        for u in self.urls:
            if self.isInterruptionRequested():
                break
            name = os.path.basename(urllib.parse.urlsplit(u).path) or "img.jpg"
            dest = self.dest_dir / name
            k = 1
            while dest.exists():
                stem, ext = os.path.splitext(name)
                dest = self.dest_dir / f"{stem}({k}){ext or '.jpg'}"
                k += 1
            self.status.emit(f"下载 {name} …")
            try:
                data = _fetch(u, self.proxy_url, timeout=40)
                dest.write_bytes(data)
                n += 1
            except Exception as e:
                self.status.emit(f"  失败 {u}: {type(e).__name__}: {e}")
        self.done.emit(n, str(self.dest_dir))


REVERSE_ENGINES = [
    ("Google 智能镜头",
     "https://lens.google.com/uploadbyurl?url={url}"),
    ("Bing 视觉搜索",
     "https://www.bing.com/images/search?view=detailv2&iss=sbi&q=imgurl:{url}"),
    ("Yandex 图片",
     "https://yandex.com/images/search?rpt=imageview&url={url}"),
    ("SauceNAO",
     "https://saucenao.com/search.php?url={url}"),
]


def open_similar_web(parent, db, cfg, rec, dest_dir: Path) -> None:
    """入口：rec 为当前图记录（用其 source_url），dest_dir 为保存目录。"""
    from ..proxy import ProxyPool
    from ..repo import get_task

    url = (rec.source_url or "").strip()
    if not url.startswith(("http://", "https://")):
        return
    proxy_url = None
    try:
        t = get_task(db, rec.task_id) if rec.task_id else None
        pool = ProxyPool(db)
        env_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        proxy_url = pool.resolve(
            t.proxy_id if t else None,
            (t.proxy if (t and t.use_proxy) else None),
            cfg.proxy or env_proxy)
    except Exception:
        proxy_url = None

    dlg = QDialog(parent)
    dlg.setWindowTitle("在网站中查找相似")
    dlg.resize(900, 640)
    v = QVBoxLayout(dlg)
    v.addWidget(QLabel(f"来源页：{url}"))

    # ---------- 上段：同站 ----------
    gb1 = QGroupBox("同站（来源页及其上下层页面，勾选后保存）")
    v1 = QVBoxLayout(gb1)
    lst = QListWidget()
    lst.setViewMode(QListWidget.ViewMode.IconMode)
    lst.setIconSize(QSize(110, 110))
    lst.setResizeMode(QListWidget.ResizeMode.Adjust)
    lst.setSpacing(8)
    lst.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
    v1.addWidget(lst, 1)
    st = QLabel("准备抓取…")
    _tstyle(st, "QLabel{color:{text_dim};font-size:12px;}")
    v1.addWidget(st)
    row1 = QHBoxLayout()
    b_all = QPushButton("全选/全不选")
    b_save = QPushButton("保存选中…")
    _tstyle(b_save, "QPushButton{background:{accent_soft};border:none;"
                     "border-radius:6px;color:{accent_deep};font-weight:600;"
                     "padding:5px 12px;}")
    row1.addWidget(b_all)
    row1.addWidget(b_save)
    row1.addStretch()
    v1.addLayout(row1)
    v.addWidget(gb1, 3)

    # ---------- 下段：全网反查 ----------
    gb2 = QGroupBox("全网反查（浏览器打开）")
    h2 = QHBoxLayout(gb2)
    enc = urllib.parse.quote(url, safe="")
    for name, tpl in REVERSE_ENGINES:
        b = QPushButton(name)
        b.clicked.connect(
            lambda _=False, u=tpl.format(url=enc): QDesktopServices.openUrl(QUrl(u)))
        h2.addWidget(b)
    h2.addStretch()
    v.addWidget(gb2)

    worker = SiteSimilarWorker(url, proxy_url)
    url_list: list[str] = []

    def on_urls(urls):
        nonlocal url_list
        url_list = list(urls)
        for i, u in enumerate(url_list):
            it = QListWidgetItem(urllib.parse.unquote(
                os.path.basename(urllib.parse.urlsplit(u).path) or u)[:60])
            it.setData(Qt.ItemDataRole.UserRole, u)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Unchecked)
            it.setToolTip(u)
            lst.addItem(it)
        if not url_list:
            st.setText("这个页面没抓到同站图片（可能是 JS 动态渲染或需要登录）。")

    def on_thumb(idx, data):
        if idx >= lst.count():
            return
        pm = QPixmap()
        if pm.loadFromData(data) and not pm.isNull():
            lst.item(idx).setIcon(pm.scaled(
                110, 110, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation))

    def toggle_all():
        n = lst.count()
        newv = any(lst.item(i).checkState() != Qt.CheckState.Checked
                   for i in range(n))
        for i in range(n):
            lst.item(i).setCheckState(
                Qt.CheckState.Checked if newv else Qt.CheckState.Unchecked)

    def save_selected():
        sel = [lst.item(i).data(Qt.ItemDataRole.UserRole)
               for i in range(lst.count())
               if lst.item(i).checkState() == Qt.CheckState.Checked]
        if not sel:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(dlg, "保存", "先勾选要保存的图（点缩略图左上角方框）")
            return
        sw = SaveSelectedWorker(sel, Path(dest_dir), proxy_url)
        sw.status.connect(st.setText)
        sw.done.connect(lambda n, d: st.setText(f"已保存 {n} 张 → {d}"))
        self_worker = sw
        dlg.finished.connect(self_worker.requestInterruption)
        sw.start()

    worker.urls_ready.connect(on_urls)
    worker.thumb.connect(on_thumb)
    worker.status.connect(st.setText)
    worker.failed.connect(lambda m: st.setText(f"抓取失败：{m}"))
    b_all.clicked.connect(toggle_all)
    b_save.clicked.connect(save_selected)
    dlg.finished.connect(worker.requestInterruption)
    worker.start()
    dlg.exec()
