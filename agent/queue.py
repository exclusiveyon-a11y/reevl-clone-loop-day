"""수요 후보 큐 — 사람이 찾은 공개 질문 URL을 넣고, 점수로 답변할지 정한다.

점수(실행안 §6): 질문 적합도 0–3 · 검증 가능한 비교 데이터 0–3 · 최신성 0–2 · 계정·규칙 충족 0–2.
8점 이상이고 규칙 2점일 때만 작성한다. 점수가 높아도 규칙 위반이면 제외한다.
큐 파일은 저장소에 올리지 않는다(기본 data/queue.csv — .gitignore 대상). 질문 원문·닉네임은 담지 않는다.
"""
from __future__ import annotations

import csv
import os
import uuid
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))
PATH = os.environ.get("GTM_QUEUE", "data/queue.csv")
FIELDS = ["candidate_id", "channel", "source_url", "found_at", "intent", "rule_url", "rule_checked_at",
          "allowed", "score", "response_owner", "evidence_url", "placement_id", "posted_url", "status",
          "removal_reason", "title", "where", "query", "provider", "draft", "link", "draft_hash",
          "why", "posted_at", "fit", "data", "fresh", "rule"]


def _now() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M")


def load() -> list[dict]:
    """옛 CSV(필드가 적은 것)도 읽는다 — 없는 열은 빈 값으로 채운다."""
    if not os.path.exists(PATH):
        return []
    with open(PATH, newline="", encoding="utf-8") as f:
        return [{k: (r.get(k) or "") for k in FIELDS} for r in csv.DictReader(f)]


def save(rows: list[dict]) -> None:
    os.makedirs(os.path.dirname(PATH) or ".", exist_ok=True)
    with open(PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def score(fit: int, data: int, fresh: int, rule: int) -> tuple[int, bool]:
    for v, hi in ((fit, 3), (data, 3), (fresh, 2), (rule, 2)):
        if not 0 <= v <= hi:
            raise ValueError("점수 범위: fit·data 0–3, fresh·rule 0–2")
    s = fit + data + fresh + rule
    return s, (s >= 8 and rule == 2)


def add(channel: str, url: str, intent: str, fit: int, data: int, fresh: int, rule: int,
        rule_url: str = "", owner: str = "") -> dict:
    s, ok = score(fit, data, fresh, rule)
    rows = load()
    if any(r["source_url"] == url for r in rows):
        raise ValueError("이미 큐에 있는 질문입니다(중복 제거)")
    row = {k: "" for k in FIELDS}
    row.update(candidate_id=uuid.uuid4().hex[:8], channel=channel, source_url=url, found_at=_now(),
               intent=intent, rule_url=rule_url, rule_checked_at=_now() if rule_url else "",
               allowed=str(ok), score=str(s), response_owner=owner,
               placement_id=f"{channel}-{datetime.now(KST):%m%d}-{len(rows) + 1}",
               status="ready" if ok else "skip")
    save(rows + [row])
    return row


def append(row: dict) -> dict:
    """파이프라인용 — 점수·초안이 채워진 행을 그대로 넣는다. 중복 기준은 source_url."""
    rows = load()
    if any(r["source_url"] == row.get("source_url") for r in rows):
        raise ValueError("이미 큐에 있는 질문입니다(중복 제거)")
    full = {k: "" for k in FIELDS}
    full.update({k: str(v) for k, v in row.items() if k in FIELDS and v is not None})
    full["candidate_id"] = full["candidate_id"] or uuid.uuid4().hex[:8]
    full["found_at"] = full["found_at"] or _now()
    save(rows + [full])
    return full


def mark(candidate_id: str, **kv) -> dict:
    rows = load()
    for r in rows:
        if r["candidate_id"] == candidate_id:
            r.update({k: str(v) for k, v in kv.items() if k in FIELDS})
            save(rows)
            return r
    raise KeyError(candidate_id)
