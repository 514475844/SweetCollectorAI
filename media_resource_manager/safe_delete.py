"""V0.0.3：删图前先分清「软件自己的文件」和「用户原有图库的文件」。

为什么必须加这一层
------------------
导入本地图库时，软件只是把图片在磁盘上的地址登记进数据库（例如
``F:\\R\\已恢复图片\\PIC\\xxx.jpg``），**并没有把图复制进软件目录**。
老代码在「彻底删除 / 清空隔离区」时直接对这个地址做删除，于是用户在
软件里点一下删除，F 盘里的原图就没了 —— 而且不可恢复。

用户明确要求：F 盘的数据不要动。所以规则改成：

  · 文件在软件自己的数据目录里（图库 / 审核 / 隔离区等）→ 按开关正常删；
  · 文件在数据目录之外（用户原有图库、别的项目、外接盘）→ **只删登记记录，
    绝不碰磁盘文件**，并把这个事实告诉界面；
  · 开着「删除时先进回收站」却搬运失败 → 保留原文件、记错误，绝不降级成
    真删，也绝不谎报「已进回收站」。

所有删除入口统一走 ``delete_image_file()``，不许各自写 unlink。
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

LOG = logging.getLogger("mrm.safe_delete")


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except Exception:
        return False


def mark_external_root(cfg, folder) -> None:
    """把用户选来导入的文件夹记成「只读原图库」，以后删除一律绕开。

    光靠「文件在不在软件目录里」判断不够：用户完全可能把数据目录放在
    和原图库同一块盘、甚至某个软件目录是链接指向外部。导入时把源目录
    记下来，等于给用户原图再上一道保险。
    """
    if cfg is None or not folder:
        return
    try:
        root = Path(str(folder)).resolve()
        cur = [str(x) for x in (
            getattr(cfg, "external_library_roots", None) or [])]
        s = str(root)
        if s in cur:
            return
        for c in cur:
            try:
                root.relative_to(Path(c))
                return          # 已经记过更上层的目录，不必重复
            except Exception:
                continue
        cur.append(s)
        cfg.external_library_roots = cur
        try:
            from .config import save_config
            save_config(cfg)
        except Exception as e:
            LOG.warning("记录原图库目录失败（本次内存仍生效）：%s（%s）", s, e)
    except Exception as e:
        LOG.warning("记录原图库目录失败：%s（%s）", folder, e)


def is_software_file(path, cfg) -> bool:
    """这个图片文件是不是软件自己目录里的（可以删）？

    V0.0.6 收紧四道，宁可把软件自己的图误当「外部」不删，也绝不反过来：
      1. 只认绝对路径；2. 链接 / 路径解析结果跟字面不一致 → 不动；
      3. 落在用户导入过的原图库目录里 → 一律不动；
      4. 软件目录本身是链接指向别处 → 判不清，不动。
    """
    if not path:
        return False
    p = Path(str(path))
    if not p.is_absolute():
        return False
    try:
        if p.is_symlink():
            return False
        real = p.resolve()
        if str(real).lower() != str(p.absolute()).lower():
            return False
    except Exception:
        return False
    # 用户导入过的原有图库：最高优先级，永不碰
    for r in (getattr(cfg, "external_library_roots", None) or []):
        try:
            if _inside(p, Path(str(r))):
                return False
        except Exception:
            continue
    try:
        root = Path(cfg.db_path).parent
    except Exception:
        return False
    # 少数老配置把图库放在数据目录之外，但仍属于软件自己的下载目录
    dirs = [root]
    for attr in ("library_dir", "isolate_dir", "review_dir"):
        d = getattr(cfg, attr, None)
        if d:
            dirs.append(Path(d))
    for d in dirs:
        try:
            if d.is_symlink():
                continue        # 软件目录是链接 → 不拿它当可删依据
        except Exception:
            continue
        if _inside(p, d):
            return True
    return False


def delete_image_file(path, cfg, *, recycle: bool = True) -> str:
    """按开关处置一个图片文件，返回处置结果。

    返回值：
      ``missing``    文件本来就不在盘上
      ``protected``  是用户原有图库的文件 → 没动磁盘，只应删登记记录
      ``recycled``   已移进回收站目录（_purged）
      ``deleted``    软件自己的文件，已按「不进回收站」的开关永久删除
      ``failed``     处理失败，文件仍在原处（**绝不降级真删**）
    """
    if not path:
        return "missing"
    p = Path(str(path))
    if not p.exists():
        return "missing"

    # 第一道闸：外部文件一律不动
    if not is_software_file(p, cfg):
        LOG.info("保护用户原有图库文件，仅清登记记录：%s", p)
        return "protected"

    if not recycle:
        try:
            p.unlink()
            return "deleted"
        except Exception as e:
            LOG.error("删除软件文件失败，已保留原文件：%s (%s)", p, e)
            return "failed"

    try:
        trash = Path(cfg.isolate_dir) / "_purged"
        trash.mkdir(parents=True, exist_ok=True)
        dst = trash / p.name
        k = 1
        while dst.exists():
            dst = trash / f"{p.stem}({k}){p.suffix}"
            k += 1
        shutil.move(str(p), str(dst))
        return "recycled"
    except Exception as e:
        # 绝不降级成 p.unlink()：磁盘满 / 跨盘 / 权限问题都可能触发这里
        LOG.error("移入回收站失败，已保留原文件：%s (%s)", p, e)
        return "failed"
