"""Sweet 采集 AI 版 —— 无头服务端入口（DOCKER_X86_ORDER §A）。

复用现有引擎（订单 §1.1 红线：绝不另写引擎、禁止 fork 一份 Docker 专用实现）：
  · Web 服务 ：webui.WebServer(cfg, db, queue) → start() 返回 (bool, 中文说明)，
               stop() 已带 3s join（webui.py:2090/2127/2183）；
  · 任务调度 ：scheduler.queue.TaskQueue(cfg, max_concurrent) +
               enqueue_auto_tasks(q, cfg)（queue.py:85/646，只捞 auto_run=True）；
  · 数据库   ：db.Database(cfg.db_path)（db.py:134）；
  · 日志     ：logutil.setup_logging(log_dir, sink=...)（logutil.py:90），
               容器里 sink 给 sys.stderr，文件日志仍落 cfg.log_dir ⇒ A2。

两种跑法：
  python -m media_resource_manager.headless            # 调度 + Web（默认）
  python -m media_resource_manager.headless --no-web   # 只跑调度

环境变量覆盖（A4 + E15，MRM_ 前缀，**只改内存、不回写 config.json**）：
  MRM_HOME / MRM_WEB_BIND / MRM_WEB_PORT / MRM_WEB_USER / MRM_WEB_PASSWORD /
  MRM_WEB_LAN_OPEN / MRM_WEB_IP_WHITELIST / MRM_PARALLEL / MRM_TRUST_PROXY
  E15 补齐：MRM_WEB_ENABLED（Web 总开关）/ MRM_AI（全局 AI）/
    MRM_COMPLIANCE_LEVEL（合规基线 off/loose/standard/strict）/
    MRM_LOG_LEVEL（日志级别，特判：AppConfig 无此字段，直接进 setup_logging）
  MRM_INTERVAL / --interval：**只对本进程有效**，不写 config.json
    （AppConfig 没有 interval 字段，它只是 serve 子命令的 CLI 参数；
     详见 docker/README.md「环境变量一览」）。
  「允许时段」是任务级字段（db.py 建表列 allowed_hours / engine 判定），
    env 覆盖不了；按订单 §4 授权**点名跳过**，不在容器化里改调度语义。

与桌面端 cmd_serve（__main__.py:104-129）的差别：那边整段 sleep(interval)，
SIGTERM 要等满一个间隔才响应；这里 1 秒一醒的分段睡眠 + 有界收尾 ⇒ docker stop
不丢数据（A3）。这就是"必须重写入口"而不是复用 cmd_serve 的硬理由。
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from pathlib import Path

# ── A4：环境变量覆盖。键名 → (cfg 字段名, 类型转换) ──────────────────────
# 覆盖面按订单 A4：数据目录 / 端口 / web_user / web_password / 并行数 /
# web_lan_open / web_ip_whitelist，＋ 订单没写但容器必踩的 web_bind
# （config.py:254 默认 127.0.0.1，容器里不改宿主连不进来）＋ E1 的 trust_proxy。
# E15 补齐三个活旋钮（此前只能改 config.json，容器用户够不着）：
#   web_enabled（Web 总开关）/ ai_enabled_global（全局 AI）/ compliance_level
#   （合规基线，engine.py 两处合规门真读它 —— R186 只是默认拨 off）。
_ENV_MAP: dict[str, tuple[str, str]] = {
    "MRM_HOME":            ("__data_dir__", "path"),   # 特殊：走 user_data_dir 的 MRM_HOME
    "MRM_WEB_BIND":        ("web_bind", "str"),
    "MRM_WEB_PORT":        ("web_port", "int"),
    "MRM_WEB_USER":        ("web_user", "str"),        # R105 后不参与鉴权（兼容收下）
    "MRM_WEB_PASSWORD":    ("web_password", "str"),
    "MRM_WEB_LAN_OPEN":    ("web_lan_open", "bool"),
    "MRM_WEB_IP_WHITELIST": ("web_ip_whitelist", "list"),
    "MRM_PARALLEL":        ("parallel_tasks", "int"),
    "MRM_TRUST_PROXY":     ("trust_proxy", "bool"),    # E1，默认 0；实现见 webui._client_ip
    "MRM_WEB_ENABLED":     ("web_enabled", "bool"),    # E15
    "MRM_AI":              ("ai_enabled_global", "bool"),  # E15
    "MRM_COMPLIANCE_LEVEL": ("compliance_level", "str"),   # E15（值域见下方校验）
}

#: MRM_LOG_LEVEL 合法值（AppConfig 无 log_level 字段 ⇒ 特判，不进 _ENV_MAP）
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def _env_log_level():
    """MRM_LOG_LEVEL → logging 级别常量。

    没设 / 设空 → None（调用方用默认 INFO）；非法值 → SystemExit 中文报错
    （与 _ENV_MAP 里 int/bool 的口径一致：env 写错要当场说，不许静默用错级别）。
    """
    raw = os.environ.get("MRM_LOG_LEVEL")
    if raw is None or not raw.strip():
        return None
    name = raw.strip().upper()
    if name not in _LOG_LEVELS:
        raise SystemExit(
            f"环境变量 MRM_LOG_LEVEL 的值不是合法日志级别：{raw!r}"
            f"（可用 {'/'.join(_LOG_LEVELS)}）")
    return getattr(logging, name)


def _as_bool(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def apply_env_overrides(cfg):
    """把 MRM_* 环境变量覆盖到 cfg（**只改内存，不回写 config.json**）。

    不回写的理由：容器是"按 env 重建"的，把 env 值写进 /data/config.json 之后，
    下次不带 env 启动也会沿用上次的值，排障时极难解释。

    ⚠ 已知副作用（R157 起**已消除**）：旧版 WebServer.start() 在未设密码且
    未开 lan_open 时会自动生成密码并 save_config（容器里会把随机密码写回
    /data/config.json，排障时极难解释）。R157 后空密码 = 免密模式，**不再生成、
    不再写回**；容器部署要给密码就显式给 MRM_WEB_PASSWORD。
    （docker/README.md「常见问题」有说明。）
    """
    applied = []
    for env_key, (field, kind) in _ENV_MAP.items():
        raw = os.environ.get(env_key)
        if raw is None or raw == "":
            continue
        if field == "__data_dir__":
            # 数据目录由 config.user_data_dir() 统一出口处理（它已支持 MRM_HOME），
            # 这里只做校验：目录不存在就建，不可写就中文报错。
            d = Path(raw).expanduser()
            try:
                d.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                raise SystemExit(f"数据目录无法创建：{d}（{e}）")
            if not os.access(d, os.W_OK):
                raise SystemExit(
                    f"数据目录不可写：{d}。请在宿主机执行 "
                    f"sudo chown -R 1000:1000 <宿主数据目录>"
                )
            applied.append(f"数据目录={d}")
            continue
        try:
            if kind == "int":
                val = int(raw)
            elif kind == "bool":
                val = _as_bool(raw)
            elif kind == "list":
                val = [x.strip() for x in raw.split(",") if x.strip()]
            else:
                val = raw
        except ValueError:
            raise SystemExit(f"环境变量 {env_key} 的值不是合法{kind}：{raw!r}")
        if field == "compliance_level" and val not in _compliance_levels():
            raise SystemExit(
                f"环境变量 {env_key} 的值不是合法合规基线：{raw!r}"
                f"（可用 {'/'.join(sorted(_compliance_levels()))}）")
        setattr(cfg, field, val)
        applied.append(f"{field}={val}")
    return applied


def _compliance_levels():
    """合法合规基线集合（懒加载，避免模块级 import 形成环）。"""
    from .config import COMPLIANCE_LEVELS
    return COMPLIANCE_LEVELS


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mrm-headless", description="无头服务端（容器内运行）")
    p.add_argument("--data-dir", default=None, help="数据目录（等价 MRM_HOME，优先级更高）")
    p.add_argument("--config", default=None, help="配置文件路径（config.json）")
    p.add_argument("--port", type=int, default=None, help="Web 端口（等价 MRM_WEB_PORT）")
    p.add_argument("--bind", default=None, help="监听地址，容器内用 0.0.0.0")
    p.add_argument("--interval", type=int, default=None,
                   help="自动任务巡检间隔秒（只对本进程有效，不写 config.json）")
    p.add_argument("--no-ai", action="store_true",
                   help="关闭全局 AI（cfg.ai_enabled_global=False）")
    p.add_argument("--no-web", action="store_true", help="只跑调度器，不起 Web")
    return p


def headless_main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # A5：CLI 优先级 > 环境变量 > config.json
    if args.data_dir:
        os.environ["MRM_HOME"] = args.data_dir
    if args.port is not None:
        os.environ["MRM_WEB_PORT"] = str(args.port)
    if args.bind:
        os.environ["MRM_WEB_BIND"] = args.bind
    if args.interval is not None:
        os.environ["MRM_INTERVAL"] = str(args.interval)

    from .config import load_config
    from .db import Database
    from .logutil import setup_logging, get_logger

    # R152-E14：--config 原来是死参数（命令行定义了、主流程从不接）——
    # CLI 指定的配置文件路径必须真生效：优先级同 --data-dir（CLI > env
    # > config.json 默认路径）。None 时 load_config 自己回落到
    # user_data_dir()/config.json，行为不变。
    cfg = load_config(Path(args.config) if args.config else None)
    applied = apply_env_overrides(cfg)
    if args.no_ai:
        cfg.ai_enabled_global = False
        applied.append("ai_enabled_global=False")
    # MRM_INTERVAL / --interval：只对本进程有效（AppConfig 无 interval 字段，
    # to_dict 走 asdict 不会带它）⇒ 直接从 env 读，不 setattr 进 cfg。
    interval = (int(os.environ.get("MRM_INTERVAL") or 0)
                or int(getattr(cfg, "interval", 0) or 0) or 3600)

    # A2：ensure_dirs 必须先于 setup_logging —— 首次启动时日志目录还不存在，
    # 文件 sink 会静默建不出来；目录用 cfg.log_dir（config.py:209，ensure_dirs
    # 会建它），不现拼 db_path.parent/logs（那会造出第二个日志目录）。
    # E15：MRM_LOG_LEVEL 只对本进程有效（AppConfig 无 log_level 字段，
    # 不进 _ENV_MAP；非法值在 _env_log_level 里就 SystemExit 了）。
    _lvl = _env_log_level()
    cfg.ensure_dirs()
    setup_logging(cfg.log_dir, sink=sys.stderr,
                  level=_lvl if _lvl is not None else logging.INFO)
    log = get_logger("mrm.headless")
    log.info("无头服务端启动：%s", "；".join(applied) or "（无 env 覆盖，用 config.json 原值）")
    if _lvl is not None:
        log.info("日志级别：%s（MRM_LOG_LEVEL 只对本进程有效，不写入 config.json）",
                 logging.getLevelName(_lvl))
    log.info("巡检间隔：%d 秒（MRM_INTERVAL 只对本进程有效，不写入 config.json）", interval)

    # R161：登记生效配置 + 注入主密码；加密开启但无密码则拒绝启动
    from .config import set_active_config, set_db_master_password
    set_active_config(cfg)
    _db_pw = os.environ.get("MRM_DB_PASSWORD", "") or ""
    set_db_master_password(_db_pw)
    if getattr(cfg, "encryption_enabled", False) and not _db_pw:
        log.error("数据库已开启加密但未提供主密码（设置 MRM_DB_PASSWORD 环境变量后重启）。")
        sys.exit(2)
    db = Database(cfg.db_path)

    # ── A1：调度器 + Web（复用现成引擎，不另写）────────────────────────
    from .scheduler.queue import TaskQueue, enqueue_auto_tasks
    from .webui import WebServer

    q = TaskQueue(cfg, max_concurrent=int(getattr(cfg, "parallel_tasks", 3) or 3))
    q.start()
    # 2026-10-28（编号 21/22）：容器里也一样要有订阅监控（Web 是唯一入口）。
    try:
        from .subscribe import start_global_watcher
        start_global_watcher(cfg, cfg.db_path, q)
    except Exception as _ex:                             # noqa: BLE001
        log.warning("订阅监控启动失败（不影响采集）: %s", _ex)

    # ── A3：优雅退出（docker stop 不得丢数据）──────────────────────────
    # 现状：media_resource_manager/*.py 里 signal/SIGTERM 零命中 ⇒ 不加这个
    # 处理器，docker stop 就是强杀。分段睡眠保证信号能及时醒。
    stopping = {"flag": False}

    def _on_term(signum, _frame):
        if stopping["flag"]:
            return                      # 第二次信号：不再等待，立即走
        stopping["flag"] = True
        log.info("收到退出信号（%s），正在收尾：停止接收新任务 → 等待在跑任务（有界）→ 落盘",
                 signum)

    for s in (signal.SIGTERM, signal.SIGINT, getattr(signal, "SIGHUP", signal.SIGTERM)):
        try:
            signal.signal(s, _on_term)
        except (ValueError, OSError):
            pass                        # 非主线程/平台不支持就跳过，不硬崩

    rc = 0
    server = None
    try:
        if not args.no_web:
            server = WebServer(cfg, db, queue=q)
            ok, msg = server.start()
            if not ok:
                # 容器里 Web 是唯一操作入口：起不来就显性退出（退出码 3）。
                # 配合 compose 的 restart: unless-stopped 会重启，用户看 logs
                # 就知道原因；不选"静默继续跑"——那会让 docker ps 显示正常而
                # 实际不可用（假绿，比崩掉更糟）。
                log.error("Web 服务启动失败：%s", msg)
                log.error("容器内 Web 是唯一操作入口，本次以退出码 3 结束"
                          "（compose 的 restart: unless-stopped 会自动重启；"
                          "请按日志里的中文提示处理）")
                return 3
            log.info("Web 服务已启动：%s", msg)

        while not stopping["flag"]:
            try:
                n = enqueue_auto_tasks(q, cfg)
                snap = q.snapshot()
                if n or snap.get("running"):
                    log.info("巡检：新入队=%d 运行中=%d", n, len(snap.get("running") or []))
                for tid, reason in (snap.get("failed") or {}).items():
                    log.warning("任务 %s 失败：%s", tid, str(reason)[:120])
            except Exception as e:
                # §1.4：任何异常都不得硬停，记中文日志继续
                log.error("巡检出错（已跳过本轮）：%s", e)
            # 分段睡眠，保证收到信号能及时醒（不用一次 sleep 一小时）
            deadline = time.time() + interval
            while time.time() < deadline and not stopping["flag"]:
                time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        # 有界等待：给在跑任务一个上限，超时也要退（否则 docker stop 卡 10s
        # 后被 SIGKILL，那才是真的可能丢数据）。
        try:
            waited = 0.0
            snap = q.snapshot()
            running_ids = list((snap.get("running") or {}).keys())
            if running_ids:
                log.info("收尾：等待 %d 个在跑任务（%s）", len(running_ids), running_ids)
            while waited < 30.0:
                snap = q.snapshot()
                if not (snap.get("running") or []):
                    break
                time.sleep(0.5)
                waited += 0.5
            if waited >= 30.0 and (q.snapshot().get("running") or []):
                # 超时必须留中文警告，否则 F7「任务状态不乱」没法解释
                log.warning("收尾超时（30s），已强制退出；可能有任务停留在运行中，"
                            "下次启动会自动复位（任务不会丢，状态以数据库为准）")
            elif waited:
                log.info("在跑任务收尾完成，等待 %.1fs", waited)
        except Exception as e:
            log.warning("收尾检查出错：%s", e)
        try:
            q.stop()
        except Exception as e:
            log.warning("停止调度器出错：%s", e)
        if server is not None:
            try:
                server.stop()
            except Exception as e:
                log.warning("停止 Web 服务出错：%s", e)
        try:
            db.close()                  # flush DB
        except Exception as e:
            log.warning("关闭数据库出错：%s", e)
        log.info("已退出")
    return rc


def main():
    sys.exit(headless_main())


if __name__ == "__main__":
    main()
