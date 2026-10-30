"""게시 보호 장치 — 실제 등록 직전에 걸린다. 도배·중복·관계 공개 누락을 코드로 막는다.

- 카페별 하루 상한(기본 2건): 부동산스터디 운영 가이드의 '도배 강퇴' 규칙 때문. GTM_CAFE_DAILY_CAP로 바꿀 수 있다.
- 같은 카페 최소 간격(기본 20분): GTM_MIN_GAP_MIN.
- 같은 링크 재게시 차단: 한 번 올린 비교 링크를 다른 글에 또 붙이지 않는다.
- 관계 공개 문구('제가 운영하는'류)가 없으면 막는다.
사용자가 건별로 상한을 넘겨 올리라고 명시한 경우에만 GTM_GUARD_OVERRIDE=1을 켠다(10-30에 사용자 지시로 하루 7건을 올린 기록이 있다 —
이 장치는 그 뒤에 만들었다). 결과는 data/guard_log.jsonl에 남긴다(로컬).
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime

from .queue import KST, load

_DISCLOSE = re.compile(r"제가 (만든|운영하는)|내가 (만든|운영하는)")
LOG = os.environ.get("GTM_GUARD_LOG", "data/guard_log.jsonl")


def _cafe(url: str) -> str:
    m = re.search(r"cafe\.naver\.com/([A-Za-z0-9_-]+)/", url or "")
    return m.group(1) if m else (url or "")


def check(url: str, text: str, now: datetime | None = None) -> list[str]:
    now = now or datetime.now(KST)
    cap = int(os.environ.get("GTM_CAFE_DAILY_CAP", "2"))
    gap = int(os.environ.get("GTM_MIN_GAP_MIN", "20"))
    today = now.strftime("%Y-%m-%d")
    cafe = _cafe(url)
    rows = [r for r in load() if r["status"] == "posted" and _cafe(r["source_url"]) == cafe]
    issues = []
    n = sum(1 for r in rows if (r["posted_at"] or "")[:10] == today)
    if n >= cap:
        issues.append(f"오늘 이 카페에 이미 {n}건 게시(상한 {cap}건)")
    last = [r["posted_at"] for r in rows if len(r["posted_at"] or "") >= 16]
    if last:
        try:
            dt = datetime.strptime(max(last), "%Y-%m-%d %H:%M").replace(tzinfo=KST)
            if (now - dt).total_seconds() < gap * 60:
                issues.append(f"직전 게시로부터 {gap}분 미만")
        except ValueError:
            pass
    links = set(re.findall(r"https?://\S+", text or ""))
    if links & {r["link"] for r in load() if r["status"] == "posted" and r["link"]}:
        issues.append("이미 게시한 링크를 다시 쓰려 함")
    if not _DISCLOSE.search(text or ""):
        issues.append("관계 공개 문구 없음")
    return issues


def enforce(url: str, text: str) -> dict | None:
    """막아야 하면 poster.post가 그대로 돌려줄 결과를, 통과면 None. 기록은 항상 남긴다."""
    issues = check(url, text)
    override = os.environ.get("GTM_GUARD_OVERRIDE") == "1"
    try:
        os.makedirs(os.path.dirname(LOG) or ".", exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": datetime.now(KST).isoformat(timespec="seconds"), "cafe": _cafe(url), "issues": issues,
                                "override": override}, ensure_ascii=False) + "\n")
    except OSError:
        pass
    if issues and not override:
        return {"ok": False, "why": "게시 보호 장치: " + "; ".join(issues), "human": True, "guard": True}
    return None
