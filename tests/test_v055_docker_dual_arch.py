# -*- coding: utf-8 -*-
"""V0.0.55 批1 护栏：Docker x86 + ARM 双架构配置。

钉三条口径（都是 2026-11-18 实测过的）：
  · Dockerfile 不许再写"arm64 不在范围"；
  · compose 镜像 tag 随 MRM_ARCH 走，ARM 机器不用改文件；
  · headless 的 _ENV_MAP 必须有 MRM_AI_THREADS / MRM_CONCURRENCY
    （ARM 小机器压资源全靠这两个旋钮，容器用户够不着 config.json）。
真机验证（RK3399 上构建+打标实测）不在本文件范围 —— 等机器到位。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_dockerfile_no_longer_excludes_arm64():
    s = _read("Dockerfile")
    assert "arm64 不在本单范围" not in s
    assert "x86_64 与 arm64 双架构" in s


def test_compose_image_tag_follows_arch():
    s = _read("docker-compose.yml")
    assert "mrm:${MRM_ARCH:-x86_64}" in s


def test_compose_exposes_arm_tuning_knobs():
    s = _read("docker-compose.yml")
    assert "MRM_AI_THREADS" in s and "MRM_CONCURRENCY" in s


def test_headless_env_map_has_arm_knobs():
    from media_resource_manager.headless import _ENV_MAP
    assert _ENV_MAP["MRM_AI_THREADS"] == ("ai_threads", "int")
    assert _ENV_MAP["MRM_CONCURRENCY"] == ("concurrency", "int")


def test_docker_readme_declares_dual_arch():
    s = _read("docker/README.md")
    assert "arm64 不在当前范围" not in s
    assert "双架构" in s and "buildx" in s
