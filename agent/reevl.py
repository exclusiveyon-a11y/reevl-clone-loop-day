"""리블 공개 MCP API 클라이언트 — 인증 없음, 읽기 전용, 표준 라이브러리만 쓴다.

엔드포인트는 리블이 ChatGPT·Claude 플러그인에 공개한 것과 같다. 비밀값이 필요 없다.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

ENDPOINT = os.environ.get("REEVL_MCP_URL", "https://reevl.co.kr/api/mcp")
UA = "reevl-gtm-agent/0.1 (+https://github.com/exclusiveyon-a11y/reevl-clone-loop-day)"


_MEM: dict[str, dict] = {}
_CACHE = os.environ.get("GTM_API_CACHE", "data/mcp_cache.json")
_TTL = 6 * 3600           # 같은 단지를 6시간 안에 다시 묻지 않는다 — 라이브 MCP는 ChatGPT·Claude 플러그인과 공유한다


def _disk() -> dict:
    if not _MEM.get("_loaded"):
        try:
            _MEM.update(json.load(open(_CACHE, encoding="utf-8")))
        except Exception:
            pass
        _MEM["_loaded"] = {"t": 0}
    return _MEM


def call(tool: str, **args) -> dict:
    key = tool + json.dumps(args, ensure_ascii=False, sort_keys=True)
    hit = _disk().get(key)
    if hit and time.time() - hit["t"] < _TTL:
        return hit["v"]
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}}
    req = urllib.request.Request(ENDPOINT, data=json.dumps(body).encode(), headers={
        "content-type": "application/json", "accept": "application/json, text/event-stream", "user-agent": UA})
    for attempt in (1, 2):                     # 시간 초과·망 오류는 한 번만 다시 묻는다
        try:
            time.sleep(0.25)                   # 라이브 서버에 몰아치지 않게
            res = json.load(urllib.request.urlopen(req, timeout=30))["result"]
            break
        except (TimeoutError, urllib.error.URLError, ConnectionError):
            if attempt == 2:
                raise
    out = _parse(res)
    _MEM[key] = {"t": time.time(), "v": out}
    try:
        os.makedirs(os.path.dirname(_CACHE) or ".", exist_ok=True)
        json.dump({k: v for k, v in _MEM.items() if k != "_loaded"}, open(_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass
    return out


def _parse(res: dict) -> dict:
    if res.get("isError"):
        raise RuntimeError(res["content"][0]["text"])
    return res.get("structuredContent") or json.loads(res["content"][0]["text"])


def search(q: str, sido: str | None = None, gu: str | None = None, limit: int = 5) -> list[dict]:
    args = {"q": q, "limit": limit, "fields": "cid,name,gu,dong,hh,age"}
    if sido:
        args["sido"] = sido
    if gu:
        args["gu"] = gu
    return call("search_apartments", **args)["items"]


def apartment(cid: str) -> dict:
    return call("get_apartment", cid=cid)


def trades(cid: str, limit: int = 100) -> list[dict]:
    """매매 실거래. 해제 신고는 뺀다(시세로 인용하지 않는다)."""
    items = call("get_transactions", cid=cid, limit=limit).get("items", [])
    return [t for t in items if not t.get("canceled")]
