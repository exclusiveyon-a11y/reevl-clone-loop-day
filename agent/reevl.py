"""리블 공개 MCP API 클라이언트 — 인증 없음, 읽기 전용, 표준 라이브러리만 쓴다.

엔드포인트는 리블이 ChatGPT·Claude 플러그인에 공개한 것과 같다. 비밀값이 필요 없다.
"""
from __future__ import annotations

import json
import os
import urllib.request

ENDPOINT = os.environ.get("REEVL_MCP_URL", "https://reevl.co.kr/api/mcp")
UA = "reevl-gtm-agent/0.1 (+https://github.com/exclusiveyon-a11y/reevl-clone-loop-day)"


def call(tool: str, **args) -> dict:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}}
    req = urllib.request.Request(ENDPOINT, data=json.dumps(body).encode(), headers={
        "content-type": "application/json", "accept": "application/json, text/event-stream", "user-agent": UA})
    res = json.load(urllib.request.urlopen(req, timeout=30))["result"]
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
