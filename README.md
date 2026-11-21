# Media Resource Manager

本地优先、合规优先的**多级目录媒体发现与图库管理**工具。两条下载路径，自动选择：

1. **gallery-dl（推荐主路径）**——直接调用成熟开源下载器，它已内置大量站点的多级相册发现、
   原图挑选、重试、限速、代理、去重归档能力。我们不重复造轮子，只做编排。
2. **自研通用采集器（降级路径）**——gallery-dl 没有提取器的自定义站点，走内置
   BFS/DFS 递归发现 + 用户自定义适配器（CSS/XPath/正则）+ 并发断点续传下载。

两条路径统一汇入：**去重(SHA256+pHash) → 黑名单 → 本地AI筛选 → SQLite 落库 → 运行报告**。

> 合规底线：不绕过登录/付费墙/DRM/验证码/地理限制；不预置任何站点规则；
> 采集前检查 robots.txt 并要求填写授权依据；AI 与去重全部本地运行，不上传数据。

---

## EXE（双击即用，不用命令行）

```
D:\Sweet采集AI版\dist\SweetWeb.exe
```

- **双击桌面快捷方式「Sweet采集AI版」** → 打开 `SweetWeb.exe` 的内置窗口
  网页界面（左侧导航、任务 / 图库 / 导入与导出等页面），无黑窗。
  V0.0.36 起旧桌面 exe 已废除，交付目录里只有这一个可执行文件。
- 也可以双击项目根目录的 **`run.bat`**（源码版启动）。
- 桌面快捷方式在界面里一键创建：`设置 → 创建桌面快捷方式`（真 .lnk，
  再点一次会问你是删掉还是保留）；带参数运行 `SweetWeb.exe` 可执行命令行子命令。

> 完整的图文操作说明见 **[`docs/Sweet采集AI版使用文档.md`](docs/Sweet采集AI版使用文档.md)**。

重新打包（改动代码后）：

```bash
"$PY" -m PyInstaller build/pyinstaller.spec --noconfirm \
      --distpath D:/c/mrm_scratch_<时间戳>/dist \
      --workpath D:/c/mrm_scratch_<时间戳>/build
# 构建完再 cp -r 覆盖 dist/
```

> 别在 `dist/` 原地打包：PyInstaller 的 COLLECT 会整目录删旧输出，会被系统删除守卫拦住。

---

## 界面功能

**左栏（窄，Windows 风格）**：任务列表 + 运行选中 / 运行自动任务 / 暂停继续 /
重试失败 / 刷新 / **开机自启开关** + 队列状态（运行/待跑/失败数）。

**「任务」标签页**
- **适配器下拉框**：自动列出 `rules/` 目录里已有的规则文件
- **输出目录「浏览…」按钮**：弹目录选择器
- **代理**：下拉选择已保存代理 + 「管理…」弹窗（增删/启停/健康检查）+「测试」按钮
- 最大深度 / 并发 / 延迟 / **优先级** / 关键字 / 黑名单
- **本地AI**：启用开关 + 主题词 + 阈值
- 调度：自动运行（勾选后会被定时任务包含）
- 新增 / 保存修改 / 删除任务

**「图库」标签页**：缩略图网格、按标签/文件名/来源搜索、收藏、双击在资源管理器中定位文件。

**「日志」标签页**：实时运行日志（含每个任务的下载/去重/AI过滤统计），可清空/导出。

---

## 代理（HTTP / HTTPS / SOCKS5）

```bash
SweetWeb.exe proxy add --name office --scheme socks5 --host 127.0.0.1 --port 10808
SweetWeb.exe proxy add --name corp --scheme http --host 10.0.0.1 --port 8080 --user bob --password secret
SweetWeb.exe proxy list          # 密码自动打码
SweetWeb.exe proxy check         # 连通性+延迟探测
SweetWeb.exe proxy remove 2
```

- 支持 **http / https / socks5**（httpx 不支持 socks4，会明确拒绝）
- **多代理池**：按 失败次数 → 延迟 → 优先级 自动挑选；失败自动切换
- **健康检查**：记录延迟与最近成功时间，连续失败 3 次自动停用
- **按任务绑定**：任务可指定用哪个代理，或留空由池自动挑
- 密码在日志/列表里一律打码

## 任务队列

- **优先级**（1-9，小=先跑）、**并发数**可配置
- 状态持久化到数据库：排队 / 运行 / 暂停 / 完成 / 失败（含失败原因）
- **失败队列**：失败任务不会丢，点「重试失败」一键重跑
- 批量入队用 `submit_many`，保证优先级真实生效（逐个提交会被 worker 抢跑）

```bash
SweetWeb.exe queue --concurrency 2      # 跑所有自动任务
SweetWeb.exe serve --interval 3600      # 后台常驻，每小时补一次队列
```

## 本地 AI（已接进采集流程）

下载后的每张图都会用本地 CLIP（ONNX，**不需要 torch**，完全离线）打分：

- 打分结果写入数据库 `tags` / `score` 字段
- 达不到阈值 → **移入隔离区**（可恢复，绝不删除）
- 没装模型时不会误判，而是完全不做筛选（保住你的下载）

模型位于 `%APPDATA%\media-resource-manager\models\clip\`（154MB），
丢失可重下：`python tools/fetch_model.py --proxy http://127.0.0.1:10808`

## 审核队列 / 统计 / SSIM / 导入导出 / 深色模式

- **审核标签页**：隔离区图片逐张判断——喜欢 / 不喜欢 / 跳过 / 加入黑名单；
  决策实时写入**本地偏好模型**（按任务存权重，界面底部可看当前权重），越用越准。
- **统计标签页**：图库张数、磁盘占用、格式分布、任务分布、**重复率**、平均AI分、
  失败任务及原因；一键导出 JSON 报告。
- **图库「查找相似(SSIM)」**：本地聚类相似图，每组保留最大的一张，其余进隔离区（可恢复）。
- **导入导出**：`tasks export/import`（JSON）、`export report/metadata`（JSON/CSV）；
  GUI 里也有对应按钮。
- **规则版本回滚**：每次导入适配器自动存版本，`rules list` / `rules rollback --name x --version N`。
- **深色模式**：左栏「深色模式」按钮即时切换，配置持久化。
- **完成通知**：任务跑完托盘气泡提醒。
- **去重策略升级**：重复时**保留分辨率/体积最大的一张**，小的进隔离区，绝不直接删。

## 图标与桌面快捷方式（软件内一键创建）

- 应用图标（exe/窗口/托盘）：`media_resource_manager/resources/icon.ico`——
  **把你自己的 favicon.ico 覆盖到这个路径，重新打包即全部生效**。
- GUI 左栏「桌面快捷方式」按钮：一键在桌面创建/删除启动器。
  > 说明：写真正的 .lnk 需要 WScript.Shell COM（本机安全策略禁止），且 .url 对
  > 中文路径的百分号编码解析不了，所以用的是带 `chcp 65001` 的 .bat 启动器，双击即开、无黑窗常驻。

## 开机自启

GUI 点「开机自启」按钮切换，或命令行：

```bash
SweetWeb.exe autostart install   # 登录时启动后台调度
SweetWeb.exe autostart status
SweetWeb.exe autostart remove
```

实现方式是往「启动」文件夹放一个 .bat（免管理员、免 COM）。
> 注意：原本想用 `schtasks.exe`，但被本机安全策略列入黑名单，故改用启动文件夹方案。

---

## 环境

```bash
PY=C:/Users/Admin/.workbuddy/binaries/python/envs/default/Scripts/python.exe
```

已装：gallery-dl 1.32.13、Pillow、imagehash、scipy、opencv 5.0、onnxruntime 1.30、
tokenizers、numpy、PySide6 6.11.2、httpx、bs4、lxml、PyYAML、loguru、socksio、pytest。

> 本机 pip 默认清华源不可用，直连官方源仅 ~23 kB/s。**加代理后 ~7 MB/s**：
> `"$PY" -m pip install -i https://pypi.org/simple --proxy http://127.0.0.1:10808 <包名>`

## 验证

```bash
"$PY" -m pytest -q                    # 145 passed
"$PY" tools/selfcheck.py              # 12 项界面/流程自检
"$PY" tools/verify_stream.py          # 流式下载端到端
"$PY" tools/smoke.py                  # 多级发现→下载→去重→落库 → PASS
"$PY" tools/test_queue.py             # 队列：优先级/并发/双任务 → PASS
"$PY" tools/test_ai.py                # AI：打标签 + 阈值路由
"$PY" tools/diag_gallery_issues.py    # 图库体检（只读）
```

> `tools/validate_adapters.py`、`tools/check_pictoa_regression.py` 需要**联网 + 代理**
> （`http://127.0.0.1:10808`）；代理没开会直接连不上，这不是代码问题。

## 已知限制

- 无安装包（本机没装 Inno Setup），为便携文件夹版。
- CLIP 模型未打进 exe，依赖 `%APPDATA%` 里那份。
- 窗口固定大小、不可最大化/不可缩放、分隔条禁拖（刻意设计，防误拖）。
- 任务表单里的「关键词 / 黑名单」输入框已移除，改用「本地AI 主题词 / 排除词」；
  旧数据里的 `keywords` / `blacklist` 仍被引擎读取，导入老任务时注意。
- 尚未做：统计仪表盘的 ETA / 失败原因分类、Web UI。
