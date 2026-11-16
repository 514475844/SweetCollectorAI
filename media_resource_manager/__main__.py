"""CLI entry point: fully-automatic operation from the terminal.

Examples
--------
  mrm init
  mrm add-rule ./my-site.yaml
  mrm add-task --name "mysite" --adapter mysite --entry https://example.com/gallery \\
               --keywords cat dog --blacklist "spam" --depth 3 --out D:/pics
  mrm run 1
  mrm list
"""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
from pathlib import Path

from .config import AppConfig, load_config
from .db import Database
from .engine import Engine
from .repo import list_tasks, save_task
from .models import TaskConfig, TaskStatus


def _db(cfg: AppConfig) -> Database:
    return Database(cfg.db_path)


def cmd_init(cfg: AppConfig, args):
    cfg.ensure_dirs()
    Database(cfg.db_path)
    print(f"Initialized at {cfg.db_path}")


def cmd_add_rule(cfg: AppConfig, args):
    src = Path(args.path)
    if not src.exists():
        print("rule file not found", file=sys.stderr)
        return 1
    cfg.rules_dir.mkdir(parents=True, exist_ok=True)
    dest = cfg.rules_dir / src.name
    shutil.copy(src, dest)
    db = _db(cfg)
    from .taskio import save_rule_version
    v = save_rule_version(db, src.stem, dest.read_text(encoding="utf-8"),
                          "imported via CLI")
    print(f"Rule '{src.stem}' v{v} imported to {dest}")


def cmd_add_task(cfg: AppConfig, args):
    t = TaskConfig(
        id=None, name=args.name, adapter_name=args.adapter,
        entry_urls=args.entry, keywords=args.keywords or [],
        blacklist=args.blacklist or [], proxy=args.proxy,
        use_proxy=bool(args.proxy), concurrency=args.concurrency,
        max_depth=args.depth, delay=args.delay, output_dir=args.out,
        auto_run=args.auto, status=TaskStatus.IDLE,
    )
    db = _db(cfg)
    tid = save_task(db, t)
    print(f"Task {tid} created: {args.name}")


def cmd_list(cfg: AppConfig, args):
    db = _db(cfg)
    for t in list_tasks(db):
        print(f"[{t.id}] {t.name} adapter={t.adapter_name} "
              f"depth={t.max_depth} auto={t.auto_run} status={t.status.value}")


def cmd_run(cfg: AppConfig, args):
    db = _db(cfg)
    eng = Engine(cfg, db)
    rep = asyncio.run(eng.run(args.task_id, basis=args.basis))
    print("=== Run report ===")
    for k, val in rep.__dict__.items():
        print(f"  {k}: {val}")


def cmd_gui(cfg: AppConfig, args):
    from .gui import launch
    launch()


def cmd_serve(cfg: AppConfig, args):
    """Background scheduler: periodically enqueues auto_run tasks."""
    import time
    from .scheduler.queue import TaskQueue, enqueue_auto_tasks
    from .repo import list_tasks

    db = _db(cfg)
    q = TaskQueue(cfg, max_concurrent=args.concurrency)
    q.start()
    print(f"scheduler started: {args.concurrency} workers, "
          f"every {args.interval}s (Ctrl+C to stop)")
    enqueue_auto_tasks(q, cfg)
    try:
        while True:
            time.sleep(args.interval)
            n = enqueue_auto_tasks(q, cfg)
            snap = q.snapshot()
            print(f"[{time.strftime('%H:%M:%S')}] running={len(snap['running'])} "
                  f"failed={len(snap['failed'])} enqueued={n}")
            for tid, reason in snap["failed"].items():
                print(f"    ! task {tid}: {reason[:80]}")
            db.close()
            db = _db(cfg)
    except KeyboardInterrupt:
        q.stop()
        print("stopped")


def cmd_queue(cfg: AppConfig, args):
    """Inspect / drive the task queue (all auto_run tasks by priority)."""
    import time
    from .scheduler.queue import TaskQueue, enqueue_auto_tasks
    db = _db(cfg)
    tasks = {t.id: t for t in list_tasks(db)}
    print(f"{'id':>3} {'name':<18} {'pri':>3}  {'status':<8} note")
    for tid, t in sorted(tasks.items()):
        print(f"{tid:>3} {t.name[:18]:<18} {t.priority:>3}  "
              f"{t.status.value:<8} {t.last_error[:46]}")
    if not args.dry_run:
        q = TaskQueue(cfg, max_concurrent=args.concurrency)
        q.start()
        enqueue_auto_tasks(q, cfg, only_auto=not args.all_tasks)
        print(f"\nrunning {len(tasks)} queue items with {args.concurrency} "
              f"workers, polling for {args.wait}s ...")
        deadline = time.time() + args.wait
        while time.time() < deadline:
            time.sleep(2)
            snap = q.snapshot()
            if not snap["running"] and q.queued() == []:
                break
        snap = q.snapshot()
        print(f"\n=== result: OK={len(snap['reports'])} failed={len(snap['failed'])} ===")
        for tid, rep in snap["reports"].items():
            print(f"  task {tid}: dl={rep['downloaded']} dup={rep['dedup']} "
                  f"pages={rep['pages']} in {rep['elapsed']}s")
        for tid, reason in snap["failed"].items():
            print(f"  FAILED {tid}: {reason[:90]}")
        q.stop()
    db.close()
    return 0


def cmd_dl(cfg: AppConfig, args):
    """Direct download via gallery-dl (the recommended fast path)."""
    from .backends.gallery_dl import GalleryDLBackend, supports, installed
    if not installed():
        print("gallery-dl not installed. Run: pip install gallery-dl",
              file=sys.stderr)
        return 1
    if not supports(args.url):
        print("gallery-dl has no extractor for this URL (unsupported site).",
              file=sys.stderr)
        return 1
    dest = Path(args.out) if args.out else cfg.library_dir
    be = GalleryDLBackend(dest_root=Path(dest), proxy=args.proxy,
                          sleep=args.delay, write_metadata=True)
    files, log = be.download(args.url)
    print(f"downloaded {len(files)} file(s) -> {dest}")
    for f in files[:20]:
        print("   ", f)
    if len(files) > 20:
        print(f"    ... (+{len(files) - 20} more)")
    if not files:
        print("log:", log[-800:])
    return 0


def cmd_check(cfg: AppConfig, args):
    """Report whether the built pipeline can handle a URL (offline check)."""
    from .backends.gallery_dl import installed, supports
    print(f"gallery-dl installed: {installed()}")
    if installed():
        ok = supports(args.url)
        print(f"{'SUPPORTED' if ok else 'NOT supported'} by gallery-dl: {args.url}")
        if not ok:
            print("  -> will fall back to the built-in recursive crawler "
                  "(needs an adapter rule).")
    else:
        print("Run: pip install gallery-dl")
    return 0


def cmd_status(cfg: AppConfig, args):
    """Health-check: which optional components are actually available."""
    from .backends.gallery_dl import installed as gd_installed
    from . import VERSION as _VER
    print(f"version             : V{_VER}")
    print(f"data dir            : {cfg.db_path.parent}")
    print(f"library dir         : {cfg.library_dir}")
    print(f"gallery-dl installed: {gd_installed()}")

    for name, mod in (("onnxruntime", "onnxruntime"), ("opencv (SSIM)", "cv2"),
                      ("imagehash", "imagehash"), ("numpy", "numpy"),
                      ("Pillow", "PIL"), ("tokenizers", "tokenizers")):
        try:
            __import__(mod)
            print(f"{name:20s}: available")
        except Exception:
            print(f"{name:20s}: MISSING")

    from .classify.clip_model import LocalClassifier
    from .config import user_data_dir
    from pathlib import Path as _P
    # V0.0.3：以前只查 models/clip，而真实模型在 models/clip-vit-l14，
    # 明明能用却打印 "not found"，误导排障。现在按配置路径优先逐个找。
    cands = []
    configured = str(getattr(cfg, "ai_model_path", "") or "").strip()
    if configured:
        cands.append(_P(configured))
    cands += [user_data_dir() / "models" / n
              for n in ("clip-vit-l14", "clip-vit-b16", "clip", "clip-vit-b32")]
    model = None
    for md in cands:
        for fn in ("model_quantized.onnx", "model.onnx"):
            if (md / fn).is_file():
                model = md / fn
                break
        if model:
            break
    print(f"CLIP model file     : {model} "
          f"({round(model.stat().st_size / 1e6)} MB)" if model
          else "CLIP model file     : not found")
    clf = LocalClassifier(labels=["test", "labels"])
    print(f"classifier backend  : {clf.backend}"
          f"{'  (real local inference)' if clf.backend == 'clip' else '  (fallback mode)'}")
    return 0


def _proxy_cmd(cfg: AppConfig, args) -> int:
    from .db import Database
    from .proxy import ProxyPool, ProxyEntry

    db = Database(cfg.db_path)
    pool = ProxyPool(db)

    if args.proxy_cmd == "list":
        rows = pool.list(only_enabled=False)
        if not rows:
            print("no proxies configured")
            return 0
        print(f"{'id':>3} {'name':<16} {'enabled':<8} {'health':<8} {'lat':>6}  url")
        for e in rows:
            print(f"{e.id:>3} {e.name:<16} {str(e.enabled):<8} "
                  f"{'OK' if e.healthy() else 'FAIL':<8} "
                  f"{e.latency_ms if e.latency_ms >= 0 else '-':>6}  {e.mask()}")
        return 0

    if args.proxy_cmd == "add":
        e = ProxyEntry(name=args.name, scheme=args.scheme, host=args.host,
                       port=args.port, username=args.user or "",
                       password=args.password or "", priority=args.priority,
                       enabled=not args.disabled)
        err = e.validate()
        if err:
            print(f"invalid: {err}", file=sys.stderr)
            return 1
        pid = pool.save(e)
        print(f"proxy #{pid} saved: {e.name} -> {e.mask()}")
        return 0

    if args.proxy_cmd == "remove":
        pool.remove(args.id)
        print(f"proxy #{args.id} removed")
        return 0

    if args.proxy_cmd == "check":
        print("probing proxies (this does NOT bypass anything, just connectivity)...")
        for name, ok, lat, err in pool.check_all(test_url=args.test_url):
            print(f"  {name:<16} {'OK' if ok else 'FAIL'}  {lat} ms  {err}")
        return 0
    return 1


def cmd_tasks_io(cfg: AppConfig, args):
    """Export / import task configurations as JSON."""
    from .taskio import export_tasks, import_tasks_file
    db = _db(cfg)
    if args.action == "export":
        n = export_tasks(db, Path(args.out))
        print(f"exported {n} task(s) -> {args.out}")
        return 0
    n = import_tasks_file(db, Path(args.file))
    print(f"imported {n} task(s)")
    return 0


def cmd_export(cfg: AppConfig, args):
    """Export a library report or image metadata."""
    from .taskio import export_metadata, export_report
    db = _db(cfg)
    if args.what == "report":
        p = export_report(db, cfg.library_dir, Path(args.out))
    else:
        p = export_metadata(db, Path(args.out), fmt=args.fmt)
    print(f"written: {p}")
    return 0


def cmd_rules(cfg: AppConfig, args):
    """List rule versions / roll back to an older one."""
    from .taskio import rollback_rule
    db = _db(cfg)
    if args.rules_cmd == "list":
        for r in db.query(
                "SELECT name, version, created_at, note FROM rules "
                "ORDER BY name, version DESC"):
            print(f"  {r['name']:<20} v{r['version']:<4} {r['created_at']}  "
                  f"{r['note'] or ''}")
        return 0
    p = rollback_rule(db, cfg.rules_dir, args.name, args.version)
    print(f"rolled back '{args.name}' to v{args.version}: {p}")
    return 0


def cmd_autostart(cfg: AppConfig, args):
    """Install/remove/query the Windows logon auto-start task."""
    from . import autostart
    if args.action == "status":
        ok, detail = autostart.status()
        print(f"autostart installed: {ok}")
        print(f"  {detail}")
        return 0
    if args.action == "install":
        ok, out = autostart.install(interval=args.interval,
                                    concurrency=args.concurrency)
        print("installed" if ok else "FAILED")
        print(out.strip()[:400])
        return 0 if ok else 1
    if args.action == "remove":
        ok, out = autostart.remove()
        print("removed" if ok else "FAILED")
        print(out.strip()[:400])
        return 0 if ok else 1
    return 1


def _proxy_parser(sub):
    p = sub.add_parser("proxy", help="manage proxies (http/https/socks5)")
    ps = p.add_subparsers(dest="proxy_cmd", required=True)
    ps.add_parser("list").set_defaults(func=_proxy_cmd)
    a = ps.add_parser("add")
    a.add_argument("--name", required=True)
    a.add_argument("--scheme", default="http", choices=["http", "https", "socks5"])
    a.add_argument("--host", required=True)
    a.add_argument("--port", type=int, required=True)
    a.add_argument("--user")
    a.add_argument("--password")
    a.add_argument("--priority", type=int, default=0)
    a.add_argument("--disabled", action="store_true")
    a.set_defaults(func=_proxy_cmd)
    r = ps.add_parser("remove")
    r.add_argument("id", type=int)
    r.set_defaults(func=_proxy_cmd)
    c = ps.add_parser("check")
    c.add_argument("--test-url", default="https://example.com")
    c.set_defaults(func=_proxy_cmd)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mrm", description="Media Resource Manager")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init").set_defaults(func=cmd_init)
    ar = sub.add_parser("add-rule")
    ar.add_argument("path")
    ar.set_defaults(func=cmd_add_rule)
    at = sub.add_parser("add-task")
    at.add_argument("--name", required=True)
    at.add_argument("--adapter", required=True)
    at.add_argument("--entry", nargs="+", required=True)
    at.add_argument("--keywords", nargs="*", default=[])
    at.add_argument("--blacklist", nargs="*", default=[])
    at.add_argument("--proxy")
    at.add_argument("--depth", type=int, default=3)
    at.add_argument("--concurrency", type=int, default=4)
    at.add_argument("--delay", type=float, default=1.0)
    at.add_argument("--out")
    at.add_argument("--auto", action="store_true")
    at.set_defaults(func=cmd_add_task)
    sub.add_parser("list").set_defaults(func=cmd_list)
    arun = sub.add_parser("run")
    arun.add_argument("task_id", type=int)
    arun.add_argument("--basis")
    arun.set_defaults(func=cmd_run)
    adl = sub.add_parser("dl", help="download via gallery-dl (fast path)")
    adl.add_argument("url")
    adl.add_argument("--out", default=None)
    adl.add_argument("--proxy", default=None)
    adl.add_argument("--delay", type=float, default=1.0)
    adl.set_defaults(func=cmd_dl)
    achk = sub.add_parser("check", help="is this URL supported?")
    achk.add_argument("url")
    achk.set_defaults(func=cmd_check)
    sub.add_parser("status", help="component health-check").set_defaults(func=cmd_status)
    _proxy_parser(sub)
    sub.add_parser("gui").set_defaults(func=cmd_gui)
    asv = sub.add_parser("serve")
    asv.add_argument("--interval", type=int, default=3600)
    asv.add_argument("--concurrency", type=int, default=2)
    asv.set_defaults(func=cmd_serve)
    aq = sub.add_parser("queue", help="run tasks through the parallel queue")
    aq.add_argument("--concurrency", type=int, default=2)
    aq.add_argument("--wait", type=int, default=120)
    aq.add_argument("--all-tasks", action="store_true")
    aq.add_argument("--dry-run", action="store_true")
    aq.set_defaults(func=cmd_queue)
    aa = sub.add_parser("autostart", help="start with Windows (schtasks)")
    aa.add_argument("action", choices=["install", "remove", "status"])
    aa.add_argument("--interval", type=int, default=3600)
    aa.add_argument("--concurrency", type=int, default=2)
    aa.set_defaults(func=cmd_autostart)
    at = sub.add_parser("tasks", help="export/import task configs (JSON)")
    ats = at.add_subparsers(dest="action", required=True)
    ae = ats.add_parser("export")
    ae.add_argument("--out", required=True)
    ae.set_defaults(func=cmd_tasks_io)
    ai = ats.add_parser("import")
    ai.add_argument("--file", required=True)
    ai.set_defaults(func=cmd_tasks_io)
    ax = sub.add_parser("export", help="export report / metadata")
    ax.add_argument("what", choices=["report", "metadata"])
    ax.add_argument("--out", required=True)
    ax.add_argument("--fmt", default="json", choices=["json", "csv"])
    ax.set_defaults(func=cmd_export)
    ar = sub.add_parser("rules", help="adapter rule versions")
    ars = ar.add_subparsers(dest="rules_cmd", required=True)
    ars.add_parser("list").set_defaults(func=cmd_rules)
    arb = ars.add_parser("rollback")
    arb.add_argument("--name", required=True)
    arb.add_argument("--version", type=int, required=True)
    arb.set_defaults(func=cmd_rules)
    return p


def main():
    cfg = load_config()
    # R161：登记生效配置 + 注入环境变量主密码（加密开启场景）。
    from .config import set_active_config, set_db_master_password
    set_active_config(cfg)
    set_db_master_password(os.environ.get("MRM_DB_PASSWORD", "") or "")
    parser = build_parser()
    args = parser.parse_args()
    rc = args.func(cfg, args)
    sys.exit(rc or 0)


if __name__ == "__main__":
    main()
