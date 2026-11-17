# Dockerfile —— 多阶段构建（builder → runtime），对应订单 C1–C6
# 目标平台：x86_64 Linux（Debian/glibc）。arm64 不在本单范围（订单 §0）。
# 设计取舍（R101 发现 2）：故意不用 `pip install .` —— pyproject 的 packages
# 不含 web/ 与 bundled_models/ 且无 package-data ⇒ pip 装完镜像里没有前端
# 资产，Web 一起就报「Web 前端资源缺失」（E4 必红）。改走 COPY 源码树，
# 桌面构建配置零改动（§1.2 桌面零回归）。

# ---------- C1/C2 builder：装依赖，构建工具不残留 ----------
FROM python:3.11-slim AS builder

ARG USE_GPU=0

# NAS 构建补充（2026-10-30）：该网络 pypi.org 直连不通、阿里源也超时，
# 实测只有清华源可用 ⇒ 固定 PIP_INDEX_URL，其余逻辑不变。
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn

# 编译期可能需要的系统包（lxml/Pillow 若走源码编译）。装完在 runtime 段不出现：
# 编译产物在 builder 里 pip install 到 /install，runtime 只 COPY /install，
# 不带 apt 缓存与编译器 ⇒ C1「构建工具不残留」的落法。
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements-docker.txt ./

# 依赖装到独立前缀，方便 runtime 段整段 COPY。
# C5：USE_GPU=0（默认）走 CPU 版 onnxruntime；=1 才换 onnxruntime-gpu。
RUN pip install --prefix=/install -r requirements-docker.txt \
    && if [ "$USE_GPU" = "1" ]; then \
           pip install --prefix=/install --force-reinstall onnxruntime-gpu; \
       fi

# ---------- runtime ----------
FROM python:3.11-slim AS runtime

# C6：时区 + UTF-8（中文文件名不许乱码，F6）。
# 下面两项是 A4 的容器默认侧（具体 env 覆盖实现见
# media_resource_manager/headless.py 的 _ENV_MAP）：
#   ★ MRM_HOME 必须显式给，否则数据落 /app/data（容器可写层），
#     docker rm 即丢，F7「重启数据不丢」会假绿；
#   ★ MRM_TRUST_PROXY 默认 0 = 不信任 X-Forwarded-For（E1，安全默认）。
# ★ R130 补 MRM_WEB_BIND：程序默认监听 127.0.0.1（config.py:254，桌面端安全默认）。
#   容器里不改成 0.0.0.0 的话，`docker run -p 8080:8080` 只监听**容器自己的回环**，
#   宿主永远连不上；而 HEALTHCHECK 是在容器内打 127.0.0.1 的 ⇒ 容器显示 healthy、
#   页面打不开 —— 典型的假绿，比直接崩掉更难查。compose 里也显式给了同一个值，
#   两处都写不是冗余：只靠 compose 的话裸 docker run 就踩坑。
ENV TZ=Asia/Shanghai \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MRM_HOME=/data \
    MRM_TRUST_PROXY=0 \
    MRM_WEB_BIND=0.0.0.0

RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 拷贝已编译好的依赖（不含编译器与 apt 缓存）
COPY --from=builder /install /usr/local

WORKDIR /app
COPY media_resource_manager ./media_resource_manager
COPY pyproject.toml README.md ./

# E4/A1 断言：前端资产与无头入口必须在镜像里（构建期就炸，别等运行时才发现）。
# 真实布局是 web/index.html + web/static/{js,app.js css,img} + sw.js +
# manifest.webmanifest（不是订单正文简写的 web/static/* 三个平铺文件）。
RUN test -f /app/media_resource_manager/web/index.v2.html \
    && test -d /app/media_resource_manager/web/static/app \
    && test -f /app/media_resource_manager/headless.py \
    || (echo "Web 前端资产或无头入口缺失：构建中止" && exit 1)

# 2026-10-26：CLIP 模型必须在镜像里（153MB）。缺了不会报错，只会静默
# 「本地AI 降级成 stub」——图照样进，但主题词/排除词全失效，是最难发现的假绿。
RUN test -f /app/media_resource_manager/bundled_models/clip-vit-b32/model_quantized.onnx \
    && test -f /app/media_resource_manager/bundled_models/clip-vit-b32/tokenizer.json \
    || (echo "CLIP 模型缺失（bundled_models/clip-vit-b32）：构建中止" && exit 1)

# B5：非 root 运行
RUN useradd -u 1000 -m -s /bin/bash mrm
# 卷目录先建好；宿主 chown 1000:1000 后即可写（docker/README.md「常见问题」）
RUN mkdir -p /data /cache && chown -R 1000:1000 /data /cache

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

USER 1000:1000
EXPOSE 8080
VOLUME ["/data", "/cache"]

# C4：探活。⚠ 探的是 /healthz 而不是订单正文写的 /api/session：
# /api/session 在鉴权之后（webui.py「if not self._auth_ok(): return
# self._unauthorized()」先于该路由），而本套件的安全姿态是
# MRM_WEB_LAN_OPEN=0 + 必须设密码 ⇒ 不带凭证的 /api/session 返回 401，
# 容器会被永久判成 unhealthy。/healthz 在鉴权**之前**、只回 {"ok": true}，
# 是 R50 专门为探活建的端点（探活方不可能带着会话 cookie）。
# 镜像里没有 curl（slim 不带）⇒ 用 python 探。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; \
      sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status==200 else 1)" \
      || exit 1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "media_resource_manager.headless"]
