"""Claude Messages API 클라이언트 — 표준 라이브러리만, 예산 가드 내장.

- 키: ANTHROPIC_API_KEY (리블 본체 .env에서 agent.config로 읽는다. 이 저장소엔 없다)
- 구조화 출력: tool + tool_choice로 JSON을 강제하고 tool_use 블록의 input을 돌려준다.
- 시스템 지시문은 cache_control(ephemeral)로 캐싱한다(짧으면 캐싱이 안 될 뿐 오류는 아니다).
- 예산: 응답 usage × 공식 단가(USD/100만 토큰, 2026-10-24 platform.claude.com 요금표)로 data/claude_spend.json에 누적.
  호출 전에 하루 GTM_CLAUDE_DAILY_USD(기본 8)·총 GTM_CLAUDE_TOTAL_USD(기본 98)를 넘었으면 BudgetExceeded.
  ★크레딧 $100은 2026-11-07 만료 — 적극 쓰되 크레딧 안쪽 $2 여유를 남긴다. 모델별 누계도 남긴다.
"""
from __future__ import annotations

import json
import re
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from . import config

API = "https://api.anthropic.com/v1/messages"
HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"
OPUS = "claude-opus-5-5"
EXPIRES = "2026-11-07"
# USD / 100만 토큰 — inp·out·cache read(cr)·5분 cache write(cw)
PRICE = {
    HAIKU: {"inp": 0.10, "out": 0.50, "cr": 0.01, "cw": 0.125},
    SONNET: {"inp": 2.0, "out": 10.0, "cr": 0.10, "cw": 2.50},
    OPUS: {"inp": 4.0, "out": 20.0, "cr": 0.20, "cw": 5.0},
}
SPEND = os.environ.get("GTM_CLAUDE_SPEND", "data/claude_spend.json")
KST = timezone(timedelta(hours=9))


class BudgetExceeded(RuntimeError):
    pass


def available() -> str | None:
    """키가 있으면 키(참), 없으면 None."""
    return config.get("ANTHROPIC_API_KEY") or None


def _today() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def _load() -> dict:
    try:
        d = json.load(open(SPEND, encoding="utf-8"))
        d.setdefault("days", {}); d.setdefault("total", 0.0); d.setdefault("models", {})
        return d
    except Exception:
        return {"days": {}, "total": 0.0, "models": {}}


def spent() -> dict:
    d = _load()
    return {"today": round(d["days"].get(_today(), 0.0), 6), "total": round(d["total"], 6),
            "models": {k: round(v, 6) for k, v in d.get("models", {}).items()},
            "daily_cap": float(os.environ.get("GTM_CLAUDE_DAILY_USD", "8")),
            "total_cap": float(os.environ.get("GTM_CLAUDE_TOTAL_USD", "98")), "expires": EXPIRES}


def budget_check() -> None:
    s = spent()
    if s["today"] >= s["daily_cap"]:
        raise BudgetExceeded(f"오늘 Claude 예산 ${s['daily_cap']:g} 도달")
    if s["total"] >= s["total_cap"]:
        raise BudgetExceeded(f"총 Claude 예산 ${s['total_cap']:g} 도달")


def cost(model: str, usage: dict) -> float:
    p = PRICE.get(model, PRICE[SONNET])          # 모르는 모델은 비싼 쪽 단가로 센다
    u = usage or {}
    return (u.get("input_tokens", 0) * p["inp"] + u.get("output_tokens", 0) * p["out"]
            + u.get("cache_read_input_tokens", 0) * p["cr"]
            + u.get("cache_creation_input_tokens", 0) * p["cw"]) / 1_000_000


_LOCK = __import__("threading").Lock()      # 동시에 여러 글을 처리해도 지출 기록이 서로 덮어쓰지 않게


def _record(usd: float, model: str = "") -> None:
    with _LOCK:
        d = _load()
        t = _today()
        if model:
            d["models"][model] = round(d["models"].get(model, 0.0) + usd, 8)
        d["days"][t] = round(d["days"].get(t, 0.0) + usd, 8)
        d["total"] = round(d["total"] + usd, 8)
        try:
            os.makedirs(os.path.dirname(SPEND) or ".", exist_ok=True)
            json.dump(d, open(SPEND, "w", encoding="utf-8"))
        except Exception:
            pass


def _post(body: dict) -> dict:
    """HTTP 한 번(429·5xx면 한 번 더). 시험에서는 이 함수를 가짜로 바꿔 끼운다."""
    key = available()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY 없음")
    req = urllib.request.Request(API, data=json.dumps(body).encode(), headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    for attempt in (1, 2):
        try:
            return json.load(urllib.request.urlopen(req, timeout=int(os.environ.get("GTM_CLAUDE_TIMEOUT", "40"))))
        except urllib.error.HTTPError as e:
            if attempt == 1 and (e.code == 429 or e.code >= 500):
                time.sleep(2.0)
                continue
            raise RuntimeError(f"Claude API {e.code}: {e.read().decode(errors='ignore')[:160]}") from None
        except (TimeoutError, urllib.error.URLError) as e:
            if attempt == 1:
                time.sleep(2.0)
                continue
            raise RuntimeError(f"Claude API 연결 실패: {type(e).__name__}") from None
    raise RuntimeError("Claude API 실패")


def call_tool(model: str, system: str, user: str, tool: str, schema: dict,
              description: str = "", max_tokens: int = 600) -> tuple[dict, float]:
    """도구 하나를 강제로 부르게 해서 그 input(JSON)을 받는다. (결과, 이번 호출 비용 USD)."""
    budget_check()
    # ★Sonnet 5.5는 tool_choice type "tool"/"any"를 받지 않는다(2026-10-24 실측 400).
    #   그 모델은 auto + '반드시 이 도구로 답하라' 지시로 부르고, 글로 오면 JSON을 직접 꺼낸다.
    forced = model not in _NO_FORCE
    sys_text = system if forced else system + f"\n\n반드시 `{tool}` 도구를 한 번 호출해서 답하라. 도구 밖에 글을 쓰지 마라."
    body = {
        "model": model, "max_tokens": max_tokens,  # temperature는 5.5 모델에서 폐지(400)
        "system": [{"type": "text", "text": sys_text, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user}],
        "tools": [{"name": tool, "description": description or tool, "input_schema": schema}],
        "tool_choice": {"type": "tool", "name": tool} if forced else {"type": "auto"},
    }
    try:
        d = _post(body)
    except RuntimeError as e:
        if forced and "tool_choice" in str(e):
            _NO_FORCE.add(model)
            return call_tool(model, system, user, tool, schema, description, max_tokens)
        raise
    usd = cost(d.get("model", model) if d.get("model") in PRICE else model, d.get("usage", {}))
    _record(usd, model)
    for block in d.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == tool:
            return block.get("input") or {}, usd
    for block in d.get("content", []):              # 도구 대신 글로 답한 경우 — 본문 속 JSON을 꺼낸다
        if block.get("type") == "text":
            m = re.search(r"\{.*\}", block.get("text", ""), re.S)
            if m:
                try:
                    return json.loads(m.group(0)), usd
                except ValueError:
                    pass
    raise RuntimeError("Claude 응답에 도구 결과가 없음")


_NO_FORCE: set[str] = {"claude-sonnet-5-5"}
