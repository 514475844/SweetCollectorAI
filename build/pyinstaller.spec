# -*- mode: python ; coding: utf-8 -*-
# ══════════════════════════════════════════════════════════════════════
# pyinstaller.spec — MediaResourceManager（Sweet采集AI版）onedir 打包
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
# V0.0.53：关于页补全（软件介绍/版本/链接）；检查更新默认读 GitHub 最新 Release。
# V0.0.54：适配器市场（订阅源/一键安装）；移除 AI 自动生成规则入口。
# V0.0.55：打标断点修复；SSRF 保留段拦截；公众号规则本地化；批1 其他。
# V0.0.56：批2 —— 审核类别原因落库；偏好学习影子模式（默认只排序不硬拦）；
#          导出清单补来源/站点/采集时间；隔离区按保留期清理；合格线混淆矩阵+误判样例。
# V0.0.57：收件口链接提取补齐中文标点结尾（微信真机分享「标题。链接。」不再挂标点）。
# V0.0.58：所有模态弹窗 Teleport 到 body —— fixed 遮罩不再被过渡 transform 困住盖不住顶栏标题。
# V0.0.59：按钮尺寸全站统一（删 13 处局部覆盖）；入库百分比改阶段口径；「换成这个」改书面语。
# V0.0.60：AI 识别独立作业（独立进度/停止/锁），可与导入同时跑；全站「打标」改「AI 识别」。
# V0.0.61：修复 DirectML 会话并发推理闪退（sess.run 全部持锁串行）；AI 识别卡挪到图库页。
# V0.0.62：识别控件并入图库工具栏白框；删两处多余说明；窄窗控件全可见；结果卡识别入口改跳图库。
# V0.0.52：图库图片按视口装载、离屏释放；自动加载上限；内核缓存封顶 256 MB。
# V0.0.51：导入勾选持久化与作业分离；Web 访问设置折叠；启动后自动恢复；
#         备份同步页就地配置；模型库扩充（中文 CLIP / DINOv2-L）；
#         适配器提示词复制与外部 AI 回填。
# V0.0.35：导入子文件夹开关、切页进度不丢、灯箱独立舞台、全页表单间距统一。
# V0.0.36：废除旧桌面 exe，唯一可执行体改为 SweetWeb.exe（Web 壳）。
# V0.0.37：用户母版图标、自绘标题栏（彻底关闭 + 缩小到托盘）、限速时段表下拉。
# V0.0.48：全站文案审计（模型门槛能力档 + 界面专业书面化 + 护栏扩充）。
# V0.0.47：Web 访问设置说人话（允许局域网访问开关/删假登录账号/HTTPS归位）。
# V0.0.46：搜索框「按意思找」语义搜索；启动画面满 3 秒；灯箱 Teleport；
#         审核每页张数可调；.inline 全局间距。
# V0.0.45：修「点开始导入毫无反应」；导入页三步重排 + 进度/结果卡；
#         状态栏退出键常显红色胶囊；窗口 ✕ 改回收进托盘。
# V0.0.44：导入支持手打路径（UNC/挂载盘）/ WebDAV 来源 / 断点续导提示。
# V0.0.43：限速卡片内边距对齐同页其他卡片。
# V0.0.42：时段表改真表格 + 扫码入口下线。
# V0.0.41：限速状态条（现在/下次）+ AI 两页重写。
# V0.0.40：AI 两页重写（去死开关、在线字段按来源显隐、模型页去术语）。
# V0.0.39：改回系统标题栏；网页右下角「完全退出」（两步确认）。
# V0.0.38：图标改侧栏字标原图、无边框窗口圆角、标题栏四键换 Iconify 线性图标、
#         启动画面 + 主窗口淡入、侧栏子项只在任务页高亮、限速开关只留一处。
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
    # V0.0.51：queue.start() 里函数内惰性导入的启动恢复模块
    "media_resource_manager.startup_resume",
    "media_resource_manager.rule_market",
    "media_resource_manager.ssrf",
    "media_resource_manager.inbox",
        "media_resource_manager.tag_job",  # V0.0.60 独立识别作业（函数内惰性导入）
    "media_resource_manager.powerdown",
    "media_resource_manager.remote_import",
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
    # V0.0.49：网络协议导入（FTP/SFTP/SMB）全是函数内惰性导入，静态扫描抓不到。
    # paramiko 的 crypto/nacl 后端、smbprotocol 的 connection/session 都得收。
    hiddenimports += ["paramiko", "smbprotocol", "smbclient", "ftplib"]
    hiddenimports += collect_submodules("paramiko")
    hiddenimports += collect_submodules("smbprotocol")
    hiddenimports += collect_submodules("smbclient")
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

# ── V0.0.36：只出 SweetWeb.exe ────────────────────────────────────────────
# 用户定稿「废除旧的 exe，改用新版的 web 壳 exe」：从这一版起**不再构建**
# MediaResourceManager.exe（老桌面 Qt 界面）。命令行能力没丢 ——
# mrm_web_app.py 带参数时仍透传给 media_resource_manager.__main__ 的 CLI，
# 所以 proxy / queue / serve / autostart 这些子命令用 SweetWeb.exe 一样能跑。
# 只保留一个 Analysis，_internal 也只收这一份依赖，交付目录少一个 ~19MB exe。
# ── Web 壳：SweetWeb.exe（双击直接进网页界面）──
# V0.0.21：内置窗口的浏览器动作由 QtWebEngineCore 的页面/配置层接管，
# 这些同样是函数内惰性导入，静态扫描抓不到，hiddenimports 里显式列出。
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
    # 目录名只是 PyInstaller 的内部产物层，build_nodel.py 会把它拍平；
    # 交付目录里不会再出现任何名为 MediaResourceManager 的可执行文件。
    name="MediaResourceManager",
)