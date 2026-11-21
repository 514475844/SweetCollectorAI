# -*- mode: python ; coding: utf-8 -*-
# ══════════════════════════════════════════════════════════════════════
# pyinstaller.spec — MediaResourceManager（Sweet采集AI版）onedir 打包
# 当前版本 V0.0.78（护栏 test_v038_shell_polish::test_version_bumped 校验）
# V0.0.36 废除旧桌面 exe（唯一入口 = SweetWeb，护栏 test_v036 校验）
#
# 用法（必须从项目根目录执行，或在 build_nodel.py 驱动下自动执行）：
#   <venv-python> -m PyInstaller build/pyinstaller.spec --noconfirm \
#       --distpath <dist> --workpath <work>
#
# 说明：
#   · gallery-dl **不是** Python 包，是随成品旁置的 gallery-dl.exe（子进程
#     调用），由 build_nodel.py 的 _deploy_gallery_dl 部署，不进 spec。
#   · 本站规则 data/rules 由 build_nodel.py 的 _deploy_rules 部署。
#   · 资源 datas 目标路径与 resources.py / webui.py / models_lib.py 里
#     的 _MEIPASS 解析一致，必须保持 `media_resource_manager/...` 前缀。
# ══════════════════════════════════════════════════════════════════════
import os
import sys
from pathlib import Path

# spec 位于 build/ 目录，但资源/入口都在仓库根 —— SPECPATH 指向 build/，
# 仓库根因此是它的父目录。
ROOT = Path(SPECPATH).parent
BLOCK_CIPHER = "Cipher"  # 占位（无加密）
sys.setrecursionlimit(10000)

# ── 运行时数据（全部打进 _internal/media_resource_manager/...）─────────
datas = [
    # 前端（src 里 vite 已构建出的静态产物；若改动过 webapp 需先 npm run build）
    (str(ROOT / "media_resource_manager" / "web"),
     "media_resource_manager/web"),
    # 图标 / 水印 / 品牌图
    (str(ROOT / "media_resource_manager" / "resources"),
     "media_resource_manager/resources"),
    # 浏览器采集助手扩展（manifest/popup/图标）
    (str(ROOT / "extension"), "extension"),
]

# 捆绑 CLIP 模型（约 151MB，AI 本地标注必需）。
# ⚠ 必须条件化 —— PyInstaller 遇到不存在的 datas 源会直接中断打包，
# 写死会让「还没下模型」的构建机完全打不出 exe（r60c C8 钉住）。
_bundled = ROOT / "media_resource_manager" / "bundled_models"
if os.path.isdir(_bundled):
    datas.append((str(_bundled), "media_resource_manager/bundled_models"))

# 若构建机上有 models_lib 所需的额外下载模型目录也一起（可选）。
_extra_models = ROOT / "data" / "models"
if _extra_models.is_dir():
    datas.append((str(_extra_models), "media_resource_manager/data/models"))

# 静态文件名里的隐藏导入（OCR / 文本分类 / 去重等惰性或动态加载）
hiddenimports = [
    # 本地 AI 推理栈
    "onnxruntime",
    "onnxruntime.capi._pybind_state",
    "tokenizers",
    # 纯 Python、函数内懒加载 —— PyInstaller 静态扫描抓不到，必须手列，
    # 否则打包后「源码跑好好的、exe 一用就炸」（2026-11-09 护栏测试抓出）：
    #   segno      → webui 扫码登录二维码（webui.py 内 from segno import make）
    #   socksio    → httpx 的 SOCKS5 代理传输（可选依赖）
    #   cryptography.* → tls.py 开机自签 HTTPS 证书 / crx.py 读浏览器凭证，
    #     全是函数内懒加载；x509 与 ec 曲线漏一个，开了 HTTPS 就起不来
    'segno',
    'socksio',
    'cryptography.x509',
    'cryptography.hazmat.primitives.asymmetric.ec',
    'cryptography.hazmat.primitives.asymmetric.rsa',
    'cryptography.hazmat.primitives.asymmetric.padding',
    'cryptography.hazmat.primitives.hashes',
    'cryptography.hazmat.primitives.serialization',
    # V0.0.14：素材交付链路的五个模块都在函数内懒加载（桌面菜单与网页端点
    # 分别 import）。按上面同一条经验手列一次，避免「源码跑得好好的、exe
    # 一打开菜单/导出就报模块未找到」。
    # V0.0.71：AI 看图说词模块，webui 里函数内 lazy import，静态扫描抓不到。
    'media_resource_manager.auto_vocab',
    'media_resource_manager.classify.blip_caption',
    'media_resource_manager.threshold_tune',
    'media_resource_manager.export_kit',
    'media_resource_manager.gui.subs_dialog',
    'media_resource_manager.gui.cookie_dialog',
    'media_resource_manager.gui.training_kit_dialog',
    # V0.0.16：维护动作与 AI 写适配器对话框也是函数内懒加载。
    'media_resource_manager.site_ops',
    'media_resource_manager.gui.ai_adapter_dialog',
    # V0.0.17：设置中心、完整备份管理、本机配置、扫码登录、在线模型列表
    # 全部是函数内惰性导入，漏一个就只在 exe 里表现为「菜单点了没反应」。
    'media_resource_manager.online_models',
    'media_resource_manager.gui.settings_center',
    'media_resource_manager.gui.fullbackup_dialog',
    'media_resource_manager.gui.sysinfo_dialog',
    'media_resource_manager.gui.qrlogin_dialog',
    # V0.0.18：全局搜索、站点统计看板与通知/统计纯模块同样全是惰性导入。
    'media_resource_manager.searchall',
    'media_resource_manager.site_stats',
    'media_resource_manager.notify',
    'media_resource_manager.gui.searchall_dialog',
    'media_resource_manager.gui.site_stats_dialog',
    'media_resource_manager.gui.notify_dialog',
    # V0.0.19：系统级毛玻璃背板与现代化 QSS 也是函数内惰性导入。
    'media_resource_manager.gui.acrylic',
    'media_resource_manager.gui.modern',
    # V0.0.78：远程导入 / SMB / 收件箱 —— 都是函数内惰性导入，静态扫描抓不到。
    # 漏了就只在 exe 里表现为「远程导入/SMB 点了报模块未找到」。
    'paramiko',
    'smbprotocol',
    'smbclient',
    "media_resource_manager.inbox",
    ]

# 收集 onnxruntime 的所有子模块，避免动态加载缺文件
try:
    import onnxruntime as _ort
    _ort_dir = Path(_ort.__file__).resolve().parent
    for _py in sorted(_ort_dir.glob("*.py")):
        hiddenimports.append(f"onnxruntime.{_py.stem}") if _py.stem != "__init__" else None
    # 换构建机时最容易踩：requirements.txt 装成了 CPU 版，包打出来照样能跑，
    # 只是显卡后端悄悄没了。构建日志里出声提醒，别等用户装完才发现。
    if "DmlExecutionProvider" not in list(_ort.get_available_providers()):
        print("[WARN] 推理库不含 DmlExecutionProvider，本包不带显卡加速；"
              "请按 requirements.txt 在 Windows 上装 onnxruntime-directml")
except Exception:
    pass

# R105：gallery_dl 是 backends/gallery_dl.py 里 supports() 的函数内懒加载
# —— 静态扫描抓不到。漏了它 ⇒ 冻结态 supports() 恒 False（ImportError 被吞）
# ⇒ 快路径全灭、无规则任务误报「这个网址我们还不会抓」。
# extractor 必须 collect_submodules 全量收：里面有数字开头的模块名
# （如 123photo.py），普通扫描会漏。
try:
    from PyInstaller.utils.hooks import collect_submodules
    hiddenimports += collect_submodules("gallery_dl")
    hiddenimports += collect_submodules('gallery_dl.extractor')
except Exception:
    pass

binaries = []
# V0.0.34：显卡推理组件随包分发。onnxruntime-directml 的 capi 目录里
# DirectML.dll / onnxruntime.dll / onnxruntime_providers_*.dll 全是运行时
# 按名字动态加载的 —— 静态 import 扫描抓不到，漏一个打包后 providers 就
# 只剩 CPU（界面显示"未安装显卡组件"，用户还得自己去装）。显式收齐。
try:
    from PyInstaller.utils.hooks import collect_dynamic_libs
    binaries += collect_dynamic_libs("onnxruntime")
except Exception:
    pass

# ── V0.0.36 起唯一可执行体：SweetWeb.exe（Web 壳，双击直接进网页界面）──
# 旧桌面入口与旧 exe 已废除（护栏 test_v036 钉死），只用这一个 Analysis + EXE。
# 内置窗口的浏览器动作由 QtWebEngineCore 的页面/配置层接管，这些同样是函数内
# 惰性导入，hiddenimports 里已显式列出。
a_web = Analysis(
    [str(ROOT / "mrm_web_app.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports + [
        # V0.0.21：Web 壳的内置渲染内核与页面策略（函数内惰性导入，静态扫描抓不到）。
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebEngineCore",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 注意：**不**排除 QtWebEngine —— SweetWeb 的内置窗口形态靠它。
    excludes=["tkinter", "PySide6.QtQml", "PySide6.QtQuick",
              "PySide6.Qt3DCore", "PySide6.QtMultimedia"],
    noarchive=False,
)
pyz_web = PYZ(a_web.pure)
exe_web = EXE(
    pyz_web,
    a_web.scripts,
    [],
    exclude_binaries=True,
    name="SweetWeb",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(ROOT / "media_resource_manager" / "resources" / "icon.ico"),
)

coll = COLLECT(
    exe_web,
    a_web.binaries,
    a_web.datas,
    strip=False,
    upx=False,
    name="MediaResourceManager",
)