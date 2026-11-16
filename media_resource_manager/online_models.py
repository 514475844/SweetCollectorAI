"""在线接口模型清单（桌面端与网页端同一口径，纯函数、无 Qt 依赖）。

对齐 ``webui._api_online_models``：请求 OpenAI 兼容的 ``GET {base}/models``，
有密钥才带 ``Authorization: Bearer <key>``，解析 ``{data:[{id:...}]}``，
模型编号去重后排序返回。本模块只用标准库 ``urllib``；网络不可达、返回不是
JSON、字段形状异常等情况一律转成完整中文句子写入 ``error``，绝不把异常抛给
调用方。协议端点、鉴权头与响应结构与网页端保持一致，不另写一套网络口径。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request


def _err(message: str) -> dict:
    """统一的失败返回：始终只含 ok / models / error 三个键。"""
    return {"ok": False, "models": [], "error": message}


def _conn_reason(exc: BaseException) -> str:
    """把 urllib 的英文异常壳剥掉，换成能直接呈现的完整中文句子。"""
    low = str(exc).lower()
    if "timed out" in low or "timeout" in low:
        return "连接超时，请检查网络或代理配置。"
    if "refused" in low:
        return "对方拒绝连接，请检查地址与端口。"
    if "getaddrinfo" in low or "name or service" in low:
        return "域名解析失败，请检查接口地址。"
    if "tunnel" in low or "proxy" in low:
        return "代理连接失败，请检查代理服务是否在运行。"
    return "连接失败，请检查接口地址后重试。"


def fetch_models(api_url: str, api_key: str = "", timeout: float = 8.0) -> dict:
    """返回 ``{'ok': bool, 'models': list[str], 'error': str}``。

    ``api_url`` 归一化（去掉尾部斜杠）后拼接 ``/models``；``api_key`` 非空才
    添加 Bearer 鉴权头。成功时 ``models`` 为去重排序后的模型编号列表，
    ``error`` 为空串；任何失败都返回完整中文句子，且绝不抛异常到调用方。
    """
    try:
        base = str(api_url or "").strip()
        key = str(api_key or "").strip()
        if not base:
            return _err("尚未填写在线 API 地址，请填写后重试。")
        url = base.rstrip("/") + "/models"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        if key:
            req.add_header("Authorization", "Bearer " + key)
        opener = urllib.request.build_opener()
        try:
            with opener.open(req, timeout=float(timeout or 8.0)) as resp:
                raw = resp.read(2 * 1024 * 1024)
        except urllib.error.HTTPError as e:
            hint = {401: "密钥无效或权限不足",
                    403: "访问被拒绝，可能需要更换地区或代理",
                    404: "该地址未提供模型清单接口"}.get(e.code, "接口返回错误")
            return _err("%s（HTTP %d），请检查后重试。" % (hint, e.code))
        except Exception as e:                       # noqa: BLE001 —— 网络错误一律兜住
            return _err(_conn_reason(e))
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            return _err("返回内容不是有效的 JSON，请确认该接口为 OpenAI 兼容接口。")
        items = data.get("data") if isinstance(data, dict) else data
        ids: list[str] = []
        for it in (items or []):
            mid = it.get("id") if isinstance(it, dict) else it
            if mid:
                ids.append(str(mid))
        return {"ok": True, "models": sorted(set(ids)), "error": ""}
    except Exception:                               # noqa: BLE001 —— 绝不外抛
        return _err("读取在线模型清单失败，请稍后重试。")
