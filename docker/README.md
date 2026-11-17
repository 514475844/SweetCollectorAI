# Sweet 采集 AI 版 —— Linux 无头服务端 + Docker 部署文档

适用平台：**x86_64 Linux**（飞牛 FnOS / Debian / Ubuntu）。arm64 不在当前范围。
本文对应作业单 `DOCKER_X86_ORDER` 的 G1–G3。所有标 **【待实测】** 的数字必须在
真机上跑完后回填——不编数字。

---

## 一、装 Docker（G1）

Debian/Ubuntu：

```bash
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-plugin
sudo systemctl enable --now docker
```

飞牛 FnOS：见下一节。

## 二、飞牛 FnOS 注意事项（G2）

- compose 子命令在 FnOS 上可能叫 `docker compose`（v2）而不是 `docker-compose`（v1），
  两条命令本文都给出，对着能用的那条试。
- **存储路径**：FnOS 的存储池挂载点不一定是 `./data` 这种相对路径 ⇒ 建议把
  compose 里的 `./data:/data` 改成绝对路径（如 `/vol1/docker/mrm/data:/data`），
  改成你自己存储池下的路径。
- **权限**：容器以 UID 1000 运行。FnOS 上共享文件夹的属主可能不是 1000 ⇒
  必须把目录属主改成 1000，否则容器起不来，启动日志里会出现中文
  「数据目录不可写：/data」。
- 2.5G 网口 / J6412、N5105 平台无特殊驱动需求；GPU 段默认注释（不开）。

## 三、构建与启动（G1）

```bash
# 1) 先准备目录并授权（漏了这步 90% 的人会卡住）
mkdir -p ./data ./library ./cache
sudo chown -R 1000:1000 ./data ./library ./cache

# 2) 构建
docker build -t mrm:x86_64 .            # 想开 GPU：--build-arg USE_GPU=1

# 3) 启动
docker compose up -d

# 或者不用 compose，直接 docker run：
docker run -d --name mrm --restart unless-stopped \
  -p 8080:8080 \
  -v $PWD/data:/data -v $PWD/library:/data/library -v $PWD/cache:/cache \
  -e MRM_HOME=/data -e MRM_WEB_BIND=0.0.0.0 -e MRM_WEB_PORT=8080 \
  -e MRM_WEB_USER=admin -e MRM_WEB_PASSWORD=你的密码 \
  -e MRM_WEB_LAN_OPEN=0 -e MRM_TRUST_PROXY=0 \
  mrm:x86_64

# 4) 验收：探活端点（鉴权前放行，容器内也必须 200）
curl -s http://127.0.0.1:8080/healthz        # 期望 {"ok": true}
```

⚠ **关于 `/api/session`**：它在鉴权**之后**。分两种情况（R157 口径）：
- compose 默认 `MRM_WEB_PASSWORD` 留空 = **私网免密**：宿主机 curl 走网桥
  172.17.0.1（私有段），无凭证也返回 200；
- 显式设了密码之后，不带凭证的 `curl /api/session` 会返回
  **401，这是预期行为，不是故障**。要复验 F2 的「200 且 JSON 合法」，请带凭证
  `curl -u admin:你的密码 http://127.0.0.1:8080/api/session`
  （Basic Auth 用户名固定 `admin`，只有密码管事 —— R105 后不再区分用户）；
  或临时开一次免密（`MRM_WEB_LAN_OPEN=1`，验完改回 0）。
Dockerfile 的 HEALTHCHECK 也因此探 `/healthz` 而不是 `/api/session`。

## 四、卷说明（G1）

| 卷 | 内容 |
|---|---|
| `./data:/data` | 数据库、config.json、任务、日志（logs/）、模型、回收站 |
| `./library:/data/library` | 图库（采集下来的图） |
| `./cache:/data/cache` | 缓存（缩略图等，可随时清空，不影响数据）。⚠ 必须落 `/data/cache`：代码里缓存目录 = `MRM_HOME/cache`；旧写法 `./cache:/cache` 挂了个没人用的死路径 |

★ 数据目录**必须**显式给 `MRM_HOME=/data`。不给的话程序会把数据写到
`/app/data`（容器可写层），`docker rm` 一删全没——表面上"重启数据没丢"
其实是假象。

### 4.1 想从 NAS 已有的相册/下载目录导入图片（2026-10-29 新增）

网页上的「从文件夹学习 / 导入」浏览的是**容器内部**的目录，容器默认只看得见
上面那三个卷。NAS 上的其他目录（比如 `/vol1/1000/photos`）必须先挂进容器：

```yaml
    volumes:
      - ./data:/data
      - ./library:/data/library
      - ./cache:/data/cache
      # ★ 只读挂载要导入的目录（:ro 防止误删 NAS 上的原图，软件只读不写）
      - /vol1/1000/photos:/mnt/photos:ro
```

重启容器后，网页里点「浏览」就能看到 `/mnt/photos`。

**路径归属（重要）**：网页上所有路径（图库目录、任务保存目录、导入来源、
磁盘剩余空间）指的都是**运行软件那台机器**——Docker 部署时就是容器/NAS，
不是浏览器所在的电脑。目录选择器顶部会显示机器名与"并非当前浏览器所在电脑"，
看到陌生机器名属于正常现象。

## 五、端口、网络与环境变量一览

- 默认端口 8080。宿主端口改 compose 里 `"8080:8080"` 的左边。
- 容器里监听地址默认 127.0.0.1 ⇒ **必须**设 `MRM_WEB_BIND=0.0.0.0`，
  否则宿主根本连不进来（容器化最容易卡住的一条）。
- 反向代理（nginx/Caddy）后面：默认**不信任** `X-Forwarded-For`
  （`MRM_TRUST_PROXY=0`）。只有确认代理会**覆盖**（不是追加）这两个头时才设 1，
  否则别人伪造一个头就能绕过免密判定。
- 局域网免密 `web_lan_open` 程序默认 **False**（R152-E5 安全默认翻转）⇒
  本 compose 显式设 0 只是兜底。要手机免密直连再打开，并确认只在内网。

| 环境变量 | 作用 | 默认 / 备注 |
|---|---|---|
| `MRM_HOME` | 数据目录 | **必须显式给**（如 `/data`） |
| `MRM_WEB_BIND` | Web 监听地址 | 容器里必须 `0.0.0.0` |
| `MRM_WEB_PORT` | Web 端口 | `8080` |
| `MRM_WEB_USER` | （兼容）登录用户名 | R105 后**不参与鉴权**：env 收下但不影响登录，Basic Auth 用户名固定 `admin`，只有密码管事 |
| `MRM_WEB_PASSWORD` | 登录密码 | 留空 = **私网免密**（本机/局域网直连，公网仍拦；R157 起**不再**自动生成密码写回 config.json）。公网/反代暴露务必显式设置 |
| `MRM_WEB_LAN_OPEN` | 局域网免密 | 本 compose 设 `0`（安全） |
| `MRM_WEB_IP_WHITELIST` | 额外放行 IP/CIDR | 逗号分隔 |
| `MRM_TRUST_PROXY` | 信任代理头 | 默认 `0`（不信任可伪造的头） |
| `MRM_PARALLEL` | 同时运行任务数 | `3` |
| `MRM_WEB_ENABLED` | Web 服务总开关 | `1/0`（对应 `web_enabled`）；只改内存不落盘 |
| `MRM_AI` | 全局 AI 开关 | `1/0`（对应 `ai_enabled_global`）；关了不加载模型、不打标 |
| `MRM_COMPLIANCE_LEVEL` | 合规基线 | `off/loose/standard/strict`（默认 `standard`）；非法值启动即报错 |
| `MRM_LOG_LEVEL` | 日志级别 | `DEBUG/INFO/WARNING/ERROR/CRITICAL`（默认 `INFO`）；只对本进程有效 |
| `MRM_INTERVAL` | 巡检间隔（秒） | `3600`；**只对本进程有效，不写 config.json** |
| `MRM_DB_PASSWORD` | 数据库主密码 | `encryption_enabled=true` 时**必填**，缺失则无头入口拒绝启动（退出码 2）；只走 env，**不落盘** |

凭据与密钥在哪（F15 对账注记）：
- Web 密码：env `MRM_WEB_PASSWORD` 优先（**不落盘**）；GUI 设过的值落在
  `./data/config.json` 的 `web_password` 字段。
- 数据库主密码：只在 env `MRM_DB_PASSWORD`，**任何文件都不落盘**；数据库加密
  开着但 env 缺失时无头入口拒绝启动（退出码 2）。
- 加密导出文件（`.mrmx`，crypt.py）：口令在导出时自设，文件里只有 salt+密文，
  **不含口令本身** —— 口令丢了文件就解不开，无后门。

「允许时段」是**任务级**字段（建表列 + 调度器判定），环境变量覆盖不了，
本单按授权点名跳过，不改调度语义。

## 六、备份与升级（G1）

- 备份：停容器 → 打包 `./data` 与 `./library`（图库通常最大）→ 启容器。
  （「每天首次启动自动备份数据库、默认保留 7 份」是**桌面 GUI 专属**机制，
  落在桌面数据目录 `backups/` 下；**容器/无头入口没有这套机制**，
  请按上面手动备份 —— F15 对账修正。）
- 升级：`docker compose build && docker compose up -d`（卷不变 ⇒ 数据不动）。
  升级前先备份 `./data`；跨版本回退需要旧镜像。

## 七、常见问题（G3）

- **端口被占用**：改宿主端口；或 `lsof -i:8080` 找出占用者。
- **卷权限**：日志出现「数据目录不可写」⇒
  `sudo chown -R 1000:1000 ./data ./library ./cache`。
- **GPU 开关**：默认关（`USE_GPU=0`，CPU 版 onnxruntime）。要开就
  `--build-arg USE_GPU=1`，且宿主需装 NVIDIA Container Toolkit；
  2GB 显存的卡不推荐开。
- **忘了 Web 密码**：改 compose 里的 `MRM_WEB_PASSWORD` 重启即可
  （env 优先级高于 config.json，且 env 值**不落盘**）。R157 起程序**不再**
  自动生成密码写回 config.json；把 `MRM_WEB_PASSWORD` 留空重启 = 私网免密直进
  （公网仍拦），进去后再在设置里把密码改回去。
- **容器反复重启**：这是**设计行为**——Web 起不来时无头入口以退出码 3 退出，
  `restart: unless-stopped` 随即重启。`docker logs mrm` 里有中文原因
  （常见：端口被占、数据目录不可写、前端资产缺失）。
- **如何确认 gallery-dl 版本/体积**：容器内
  `docker exec mrm gallery-dl --version`；体积核对以你构建时 pip 装上的版本
  为准（本单不写死版本号，避免编数字）。
- **日志在哪**：`docker logs mrm`（stdout/stderr）；文件日志在
  `./data/logs/mrm.log`。
- **Web 打不开/404**：多半是前端资产没进镜像 ⇒
  `docker exec mrm ls /app/media_resource_manager/web/index.html`
  （Dockerfile 里有构建期断言，正常不会出现）。
- **反代之后界面不自动刷新（R154-F6 / SSE 被缓冲）**：网页的实时推送走
  `/api/events`（Server-Sent Events，长连接）。反向代理默认会**缓冲响应**，
  事件被攒在代理那一层不发，现象是「界面要手动刷新才动，但接口都正常」。
  nginx 需要：
  ```
  proxy_http_version 1.1;
  proxy_buffering off;          # 关键：不关它 SSE 一帧都收不到
  proxy_cache off;
  proxy_read_timeout 3600s;     # 长连接别被 60s 默认超时掐断
  chunked_transfer_encoding on;
  ```
  服务端已带 `X-Accel-Buffering: no`（nginx 认这个头会自己关缓冲），
  但**只有 nginx 认**；Apache / Caddy / Traefik 仍要按各自写法显式关缓冲。
  另：同时挂着的事件流上限 8 条（`SSE_MAX_CLIENTS`），超了服务端回 503，
  前端会自动退回定时轮询（不是故障）。

## 八、实测数据（F1/F9，必须真跑后填，不许估）

```
镜像体积      ：【待实测】GB（不含 CLIP 模型；订单口径 < 1.5GB）
docker build  ：【待实测】
冷启动到 200  ：【待实测】s
稳态内存 RSS  ：【待实测】MB
空闲 CPU      ：【待实测】%（挂机功耗相关；J6412/N5105 平台测）
```

> 状态说明（2026-10-14 R104）：本机（执行方）无 Docker 环境，以上五项**尚未
> 实测**，留空待真机回填；代码交付物（无头入口 / Dockerfile / compose /
> 自检脚本 / 护栏测试）已落地并通过测试。
