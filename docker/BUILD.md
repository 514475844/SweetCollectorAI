# Docker 构建说明（2026-10-27 状态）

## 现在的状态：**代码/依赖全部就绪，只差构建机放行 wsl.exe**

在本机（Windows 台式机）执行 `docker build` 会被安全策略挡住：

```
PROGRAM BLOCKED BY SECURITY POLICY - wsl.exe (C:\Windows\system32\wsl.exe)
```

Docker Desktop 的 Linux 引擎依赖 WSL2，而 `wsl.exe` 在
**安全中心 → 命令安全 → 程序黑名单** 里。这条拦截不可绕过（脚本/子 shell 都不行），
所以镜像**尚未在本机构建过**。

### 解锁步骤（一次性，约 1 分钟）

1. 打开「安全中心 → 命令安全 → 程序黑名单」；
2. 把 `wsl.exe` 移出黑名单；
3. 启动 Docker Desktop（等托盘图标变绿 / `docker ps` 有输出）；
4. 回到本仓库执行：`docker compose build`（或 `docker/build.sh`）。

## 构建后必须验的四件事

```bash
# ① 镜像体积（目标 < 1.5GB）
docker images mrm:x86_64 --format '{{.Repository}}:{{.Tag}} {{.Size}}'

# ② 起飞 + 健康检查（host 网络模式，避开网桥 IP 坑）
docker run -d --name mrm --network host \
  -e MRM_HOME=/data -e MRM_WEB_BIND=0.0.0.0 -e MRM_WEB_PASSWORD= \
  -v "$PWD/data:/data" -v "$PWD/library:/data/library" mrm:x86_64

sleep 25 && docker inspect --format '{{.State.Health.Status}}' mrm   # 期望 healthy
curl -s http://127.0.0.1:8080/healthz                                # 期望 {"ok": true}
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/v2     # 期望 200

# ③ 新版界面资源（应 200，不是 404）
curl -s http://127.0.0.1:8080/v2 | grep -o '/static/app/assets/[^"]*' | while read a; do
  printf '%s -> ' "$a"; curl -s -o /dev/null -w '%{http_code}\n' "http://127.0.0.1:8080$a"
done

# ④ 数据持久化：删容器再起，任务/图库仍在
docker rm -f mrm && docker run -d --name mrm --network host \
  -e MRM_HOME=/data -e MRM_WEB_BIND=0.0.0.0 -e MRM_WEB_PASSWORD= \
  -v "$PWD/data:/data" -v "$PWD/library:/data/library" mrm:x86_64
sleep 20 && curl -s http://127.0.0.1:8080/api/summary
```

## 已在本机用「等价环境」验过的东西（2026-10-27 01:00–05:20）

镜像构建不了，但容器里跑的那套东西我用一个**只装 `requirements-docker.txt` 的
纯净 venv**（无 PySide6）+ 按 Dockerfile 的 COPY 结构复制源码，验证过：

| 验证项 | 结果 |
|---|---|
| 依赖清单是否漏项 | ✅ 14 个模块全在（含后补的 `segno`/`cryptography`/`numpy`） |
| PySide6 是否进容器 | ✅ 不在（`importlib.util.find_spec` 为空） |
| `gallery-dl` 可执行 | ✅ venv 里有控制台脚本（Linux 镜像里是 `/usr/local/bin/gallery-dl`，`_exe()` 用 `shutil.which` 能找到） |
| CLIP 模型 | ✅ `bundled_models/clip-vit-b32/model_quantized.onnx` 153MB + tokenizer 都在；`LocalClassifier` 后端 = `clip`，加载 2.9s，单图推理 0.33s |
| 无头启动 | ✅ `/healthz` 200、`/api/session` 200（免密）、`/v2` 200 |
| 数据目录 | ✅ `MRM_HOME` 下自动生成 `config.json / mrm.db / library / logs / quarantine` |
| 体积估算 | base 130MB + site-packages ≈ 350MB + 源码 50MB + CLIP 151MB ≈ **700–800MB**（< 1.5GB 目标） |

## 已知的 Linux 侧注意点

- **gallery-dl 二进制**：镜像里用 pip 装的 `gallery-dl` 命令（**不是** Windows 的
  `vendor/gallery-dl.exe`）。`backends/gallery_dl.py::_exe()` 已按
  `sys.executable 同目录 → which → sys.prefix/bin` 的顺序找，Linux 下能命中。
- **socks 代理**：gallery-dl 子进程不吃 `socks5://`，后端已有
  `socks_to_http()` 自动转换（R173），容器里同样生效。
- **时区/中文**：Dockerfile 已设 `TZ=Asia/Shanghai` + `LANG=C.UTF-8`。
- **不要用 bridge 网络跑首轮验证**：E1 的 X-Forwarded-For 信任策略默认关闭，
  bridge 下 172.17.x.x 会让免密判定与 IP 白名单的表现和 host 模式不同。
