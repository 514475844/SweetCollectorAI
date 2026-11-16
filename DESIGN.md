# 设计文档（对应需求"输出要求"1–14）

> 范围：本工具**只做本地发现、下载、管理**你**拥有合法授权**的站点媒体。
> 不绕过登录/付费墙/DRM/验证码/地理限制/访问控制；不预置任何站点规则；
> 采集前检查 robots.txt 与站点条款；本地 AI 不上传任何数据。

## 1. 需求复述与合规风险清单

**复述**：一个桌面端、带托盘、可一键启动的本地媒体管理工具，从入口 URL 做多级目录
（列表→相册→图片页）递归发现，支持用户自定义 YAML/JSON 适配器做解析，可按关键字/本地视觉
模型筛选，按黑名单排除，并发下载并保留元数据，做精确(SHA256)+感知(pHash/SSIM)去重，任务持久化
（SQLite），支持代理（合法网络）、人工审核与偏好学习、定时/空闲自动运行、统计仪表盘、安装包与
卸载、单元测试。合规检查器在每次任务前核对 robots.txt 与授权依据。

**合规风险与替代实现**：
| 风险点 | 处理 |
|---|---|
| 绕过登录/付费墙/DRM/验证码 | 工具**不实现**任何绕过；遇到 401/403 自动暂停并提示人工确认，而非强行抓取 |
| 伪装 UA 轮换规避封锁 | 使用**单一稳定 UA**，仅标识本地工具；不做轮换 |
| 代理绕过地理/访问控制 | 代理仅作"合法网络出口"用途并记录日志；不提供规避封锁的功能开关 |
| 预置成人/盗版站点规则 | 不内置任何站点；适配器由用户自行添加并自负授权确认 |
| 未成年人/非自愿/偷拍/deepfake | 不在工具内提供任何相关采集能力；命中黑名单内容进入隔离区而非正式库 |
| 成人向内容 | 加年龄门禁 + 主密码 + 本地加密 + 隐藏模式（可选，需用户显式开启） |

## 2. 需要澄清的问题（请在落地前确认）

1. 目标站点是否均**自有或已获书面授权**、robots.txt 允许抓取？（影响合规基线）
2. 本地 AI 是否使用 CLIP/ONNX 模型？模型文件由你自备还是工具引导下载？
3. 是否需要 Windows 之外的 macOS/Linux 安装包，还是 Windows 优先？
4. 图库加密（主密码）是否必须 v1 就做，还是后续迭代？
5. "全自动"是否包含无人值守定时运行（夜间/空闲）？默认开机自启吗？
6. 是否需要网络恢复后自动续跑、断点续传的细粒度（已默认实现基础版）？

## 3. 项目目录结构
```
media-resource-manager/
├── media_resource_manager/
│   ├── config.py / models.py / db.py / repo.py / engine.py / __main__.py
│   ├── crawler/   rules.py adapter.py fetcher.py discovery.py
│   ├── downloader/ naming.py downloader.py
│   ├── dedup/     hash.py ssim.py
│   ├── classify/  clip_model.py preference.py
│   ├── compliance/ robots.py checker.py
│   ├── security/  paths.py crypto.py
│   ├── scheduler/ scheduler.py
│   ├── review/    queue.py
│   └── gui/       main_window.py tray.py
├── tests/         conftest.py test_robots.py test_naming.py test_discovery.py test_dedup.py test_engine.py
├── build/         pyinstaller.spec inno_setup.iss
├── examples/      example-adapter.yaml
├── requirements.txt pyproject.toml README.md DESIGN.md
```

## 4. 各模块职责
- **config**：路径/默认配置（user_data_dir + MRM_HOME 可覆盖），规则文件读写。
- **db/repo**：SQLite 建表与任务/图片/偏好 CRUD。
- **crawler.rules**：适配器规则 dataclass + 加载校验（无内置站点）。
- **crawler.adapter**：用 CSS(bs4)/XPath(lxml)/正则 提取链接与图片，自动解析 JSON-LD/OpenGraph；
  选择器零命中时告警（不静默失败）。
- **crawler.fetcher**：httpx 异步抓取；robots 检查、单 UA、按 host 限速、403/429 暂停、可选代理。
- **crawler.discovery**：BFS/DFS 递归发现，URL 规范化、同域限制、白/黑名单、已访问去重、深度限制、页面类型识别。
- **downloader**：并发、断点续传(Range)、重试退避、最小分辨率/体积过滤、缩略图过滤、元数据 sidecar、磁盘空间自暂停。
- **dedup**：SHA256 精确去重 + pHash 感知去重 +（可选）SSIM 聚类；删除/移动默认进隔离区可撤销。
- **classify**：本地 CLIP/ONNX 分类器（可插拔，缺模型时退化为标签读取）；偏好学习（本地权重）。
- **compliance**：robots.txt 解析 + 任务前合规检查（记录"许可依据"）。
- **security**：路径穿越防护、文件名清洗、可选主密码加密（cryptography，可降级）。
- **scheduler**：周期/空闲自动运行（仍过合规门）。
- **review**：待审核队列 + 决策应用 + 学习钩子。
- **gui**：PySide6 主窗口（左任务列表/右配置+日志+进度）+ 托盘。

## 5. 数据库表设计（详见 db.py）
`tasks`(配置+状态) · `images`(来源URL/哈希/尺寸/路径/标签/评分/黑名单/隔离) ·
`reviews`(审核决策) · `preferences`(每任务标签权重) · `rules`(适配器版本+回滚) ·
`compliance`(站点/robots_ok/许可依据/检查时间) · `audit_log`(所有敏感/破坏操作审计)。

## 6. 核心代码实现
入口 `engine.Engine.run` 串联：合规门 → 发现 → 下载 → 去重 → 黑名单 → 分类评分 → 报告。
每个 fetch 都经 `fetcher` 的 robots 检查与 403/429 暂停。关键文件：engine.py / crawler/* / downloader/*。

## 7. 适配器接口与 YAML 示例
见 `examples/example-adapter.yaml`。字段：name/base_url/allowed_domains/entry_urls/
list_page_selectors/album_link_selectors/image_page_selectors/image_link_selectors/
original_image_attrs/next_page_selectors/max_depth/delay/concurrency/proxy。
选择器支持 `kind: css|xpath|regex`，`attr` 取 href/src/data-original 等；并自动读取 og:image 与 JSON-LD。

## 8. 多级目录递归发现
`discovery.Discovery.run` 用双端队列实现 BFS（`strategy="dfs"` 可切 DFS）。每页：
规范化 URL → robots 检查 → 同域+白/黑名单过滤 → 提取子链接入队（depth+1）→ 提取图片 URL。
已访问集合去重防环；超过 max_depth 不入队。`adapter.detect_page_type` 区分列表/相册/图片页。

## 9. 本地 AI 分类与偏好学习
`classify.clip_model.LocalClassifier`：若提供本地 ONNX/CLIP 模型则推理打标签+置信度；
否则从下载 sidecar 的 `tags` 与文件名兜底（零网络）。`classify.preference.PreferenceLearner`
在 SQLite 按 (task_id,label) 累加权重；`score(tags)` 给图片打分，`learn(decision,tags)` 从
喜欢/不喜欢/黑名单更新权重；`is_uncertain` 实现"只让你审不确定样本"的主动学习。

## 10. 浏览器插件
**按你的要求已移除**（不做 Native Messaging / WebExtension）。

## 11. 合规检查器
`compliance.checker.ComplianceChecker.check`：先取 robots.txt（401/403 直接判定为受访问控制、
要求人工确认、不绕过）；根路径被 Disallow 则拒绝；否则要求用户填写"许可依据"文本并写入
`compliance` 表与审计日志。引擎在每次运行前校验，未通过且有依据才放行。成人向由 GUI 层做
年龄门禁+主密码+本地加密。

## 12. 安装包打包脚本
`build/pyinstaller.spec`（pyinstaller 打 one-folder）→ `build/inno_setup.iss`（Inno Setup 生成
Windows 安装包，普通权限安装到 Program Files，用户数据落 %APPDATA%，卸载可保留图库/配置）。
首次启动 `mrm init` 自动建库与默认目录。

## 13. 单元测试与运行示例
`tests/` 用 fixtures 页面测试（不抓真站）：robots 解析、路径穿越防护、递归发现（深度/防环/同域）、
SHA256/pHash、引擎端到端（下载+去重，且"未过合规门则拒绝"）。
运行：
```
pip install -r requirements.txt
pytest
mrm init
mrm add-rule examples/example-adapter.yaml
mrm add-task --name mysite --adapter my-gallery --entry https://... --depth 3 --out D:/pics
mrm comply 1 "我拥有该站点授权，robots.txt 允许抓取"
mrm run 1
mrm gui          # 桌面界面
mrm serve        # 后台定时自动运行
```

## 14. 使用说明与常见问题
- **Q：提示合规未通过？** 先 `mrm comply <id> "依据"`，确认 robots.txt 允许且你确有授权。
- **Q：遇到 403/429 卡住？** 工具会暂停并记日志，不会绕过；请确认授权或调高 delay/concurrency。
- **Q：去重误删？** 重复/命中黑名单的文件默认移动到 `isolate_dir`（隔离区），可手动恢复。
- **Q：本地 AI 不生效？** 未安装模型时退化为标签读取；装 onnxruntime + 自备模型后自动启用。
- **Q：磁盘满？** 剩余空间低于 200MB 自动暂停，清理后重跑即可续传。
- **Q：跨平台？** 核心为纯 Python；GUI 用 PySide6，macOS/Linux 亦可运行（安装包以 Windows 为主）。
