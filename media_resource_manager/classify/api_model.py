"""可选的在线 AI 分类后端（OpenAI 兼容 /chat/completions 接口）。

用户如果有更好的在线模型（或本地部署的兼容服务），可以在设置里填
地址/密钥/模型名，主题词与排除词筛选就会走这个后端，替代内置 CLIP。

接口约定与 LocalClassifier 一致：classify(path) -> [(label, score), ...]，
score 为 0~1 的匹配度（按分排序）。任何失败都返回 []（等于不过滤，
不会让任务失败）。
"""
from __future__ import annotations

import base64
import json
import re
import urllib.request
from pathlib import Path
from typing import Optional

_SYS = (
    "你是图片筛选助手。对给定图片，逐个评估它与下列标签的匹配程度。"
    "只输出一个 JSON 对象：{\"标签\": 分数}，分数为 0~1 的一位小数，"
    "不要输出其他任何文字。标签列表：{labels}"
)


class ApiClassifier:
    backend = "api"

    def __init__(self, labels: list[str], base_url: str, api_key: str = "",
                 model: str = "", timeout: int = 30):
        self.labels = [l for l in labels if l.strip()]
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or "gpt-4o-mini"
        self.timeout = timeout

    # ------------------------------------------------------------------
    def _post(self, payload: dict) -> dict:
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {self.api_key}"}
                        if self.api_key else {})},
            method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _score_image(self, path: Path) -> dict:
        b64 = base64.b64encode(Path(path).read_bytes()).decode("ascii")
        content = [
            {"type": "text", "text": _SYS.format(
                labels="、".join(self.labels))},
            {"type": "image_url", "image_url": {
                "url": f"data:image/jpeg;base64,{b64}"}},
        ]
        data = self._post({"model": self.model, "messages": [
            {"role": "user", "content": content}], "temperature": 0})
        # ⚠ OpenAI 标准字段是 `message.content`，`message.text` 是某些自建网关
        # 的形状。老代码只读 text → 在标准接口上永远是空串，于是「在线 AI 打标
        # 一张都不命中」，而界面上没有任何报错（分数全为空 = 不过滤）。
        # 两个都认，content 可能是分片列表（部分网关把 text 拆成数组）。
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        text = msg.get("content")
        if isinstance(text, list):
            text = "".join(str(p.get("text", "")) for p in text
                           if isinstance(p, dict))
        if not text:
            text = msg.get("text", "")
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return {}
        try:
            raw = json.loads(m.group(0))
        except Exception:
            return {}
        out = {}
        for lab in self.labels:
            for k, v in raw.items():
                if str(k).strip() == lab:
                    try:
                        out[lab] = max(0.0, min(1.0, float(v)))
                    except Exception:
                        pass
                    break
        return out

    # 与 LocalClassifier 相同的对外接口 --------------------------------
    def classify(self, path) -> list[tuple[str, float]]:
        if not self.labels or not self.base_url:
            return []
        try:
            scores = self._score_image(Path(path))
        except Exception:
            return []
        pairs = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        return pairs
