"""Claude 판정(triage)과 발행 전 심사(review) — 사람 검수를 줄이는 두 관문.

- triage: 실제 주거 질문인가 / 리블 페이지가 답이 되는가 / 댓글이 적절한가 + 조건 구조화. Haiku.
- review: 코드가 먼저 숫자·날짜·링크·공개 문구를 대조하고(실패면 Claude를 부르지 않는다), 통과하면 Haiku가 표현을 심사.
- Claude는 숫자를 만들지 않는다. 숫자는 리블 공개 API에서 코드가 가져온 facts 안에서만.
- 글 원문은 600자로 자르고 작성자 닉네임은 넣지 않는다.
"""
from __future__ import annotations

import json
import re

from . import claude

TRIAGE_SYSTEM = """너는 한국 부동산 커뮤니티 글을 분류하는 심사자다. 리블(reevl.co.kr)은 전국 아파트 실거래·입지 데이터를 무료로 보여주는 서비스이고, 운영자가 관계를 밝힌 짧은 답글로 도움이 될 때만 답한다.

판정 기준:
- is_question: 글쓴이가 실제로 주거(매매·전세·실거주·학군·통근·재건축 등) 판단을 위해 묻고 있는가. 뉴스 퍼온 글, 매물·분양 홍보, 후기·잡담, 단순 정보 공유는 false.
- relevant: 단지·지역·평형·학교·역처럼 실거래 데이터로 답할 수 있는 대상이 글에 있는가. ★이 단계는 매칭 전이다 — matched가 비어 있어도 그 이유로 낮추지 마라(매칭은 다음 단계에서 한다).
- appropriate: 댓글을 다는 것이 적절한가. 광고·매물 글, 정치·분쟁·비방, 개인정보, 법률·세무 상담, 사고·민감 사안, 이미 해결된 글이면 false.
- confidence: is_question·appropriate 판정에 대한 확신(0~1). 매칭 여부는 반영하지 않는다. 글이 잘려 있어도 질문 의도가 분명하면 높게.
- reason: 한국어 한 문장. 판정 이유만.
- entities: 글에 실제로 나온 것만. 없으면 빈 값. 추측 금지."""

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "is_question": {"type": "boolean"},
        "relevant": {"type": "boolean"},
        "appropriate": {"type": "boolean"},
        "intent": {"type": "string", "enum": ["compare", "price", "school", "jeonse", "rebuild", "commute", "other"]},
        "entities": {"type": "object", "properties": {
            "complexes": {"type": "array", "items": {"type": "string"}},
            "schools": {"type": "array", "items": {"type": "string"}},
            "stations": {"type": "array", "items": {"type": "string"}},
            "region": {"type": "string"}, "budget": {"type": "string"}, "area": {"type": "string"}}},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string"},
    },
    "required": ["is_question", "relevant", "appropriate", "intent", "confidence", "reason"],
}

REVIEW_SYSTEM = """너는 커뮤니티 답글의 발행 전 심사자다. 아래 금지 사항 중 하나라도 있으면 pass=false와 이유를 issues에 적는다.

금지:
1. 투자 권유, 매수·매도 추천("사세요", "지금이 기회" 등)
2. 가격이 오른다·내린다는 단정이나 전망
3. 학교 배정을 확정하는 표현(주변 단지 목록은 괜찮다)
4. facts에 없는 수치·사실·단지·날짜
5. 관계 공개 누락("제가 운영하는"/"내가 운영하는"/"제가 만든" 류로 리블이 자기 서비스임을 밝히지 않으면 위반)
※ 링크 개수는 'http'로 시작하는 주소의 개수다. 주소 안의 %7C 같은 인코딩 문자는 같은 링크의 일부다.
6. 링크 2개 이상
7. 과장·비방·다른 서비스 깎아내리기
8. 질문과 어긋난 답 — 답글의 단지·지역이 question이 묻는 단지·지역과 다르거나(질문에 없는 다른 도시의 단지 등), 질문의 고민에 답하지 않음.
   question이 비어 있으면 이 항목은 보지 않는다.

통과면 pass=true, issues=[]. 고칠 수 있으면 fixed_draft에 고친 전문(링크·공개 문구 유지, 숫자 변경 금지). confidence는 판정 확신(0~1)."""

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "pass": {"type": "boolean"},
        "issues": {"type": "array", "items": {"type": "string"}},
        "fixed_draft": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["pass", "issues", "confidence"],
}

_URL = re.compile(r"https?://\S+")
_AMOUNT = re.compile(r"\d+(?:\.\d+)?억|\d{1,3}(?:,\d{3})+만|\d+만")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}|(?<![\d.])\d{1,2}/\d{1,2}(?![\d/])|(?<![\d.])\d{2}\.\d{1,2}(?![\d.억만])")
_DISCLOSE = re.compile(r"제가 (만든|운영하는)|내가 (만든|운영하는)")


def code_check(draft: str, facts: list[str]) -> list[str]:
    """Claude 없이 잡는 것 — 금액·날짜가 facts에 모두 있는가, 링크 1개 이하, 관계 공개 있음."""
    issues = []
    body = _URL.sub(" ", draft)
    blob = "\n".join(facts or [])
    for a in _AMOUNT.findall(body):
        if a not in blob:
            issues.append(f"facts에 없는 금액: {a}")
    for d in _DATE.findall(body):
        if d not in blob:
            issues.append(f"facts에 없는 날짜: {d}")
    if len(_URL.findall(draft)) > 1:
        issues.append("링크가 2개 이상")
    if not _DISCLOSE.search(draft):
        issues.append("관계 공개 누락")
    return issues


def triage(post_title: str, post_text: str, channel: str, where: str, matched: dict) -> tuple[dict, float]:
    user = json.dumps({
        "channel": channel, "community": where,
        "title": (post_title or "")[:200], "text": (post_text or "")[:600],
        "matched": {"kind": matched.get("kind"), "complexes": matched.get("complexes", [])[:3],
                    "hubs": matched.get("hubs", [])[:2]},
    }, ensure_ascii=False)
    out, usd = claude.call_tool(claude.HAIKU, TRIAGE_SYSTEM, user, "triage", TRIAGE_SCHEMA,
                                "글 판정 결과를 기록한다", max_tokens=500)
    out["confidence"] = float(out.get("confidence") or 0)
    return out, usd


def review(draft: str, facts: list[str], channel: str, model: str = claude.HAIKU, question: str = "") -> tuple[dict, float]:
    issues = code_check(draft, facts)
    if issues:                                   # 코드에서 걸리면 Claude를 부르지 않는다(비용 0)
        return {"pass": False, "issues": issues, "confidence": 1.0, "by": "code"}, 0.0
    user = json.dumps({"channel": channel, "question": question[:1200], "draft": draft, "facts": facts}, ensure_ascii=False)
    out, usd = claude.call_tool(model, REVIEW_SYSTEM, user, "review", REVIEW_SCHEMA,
                                "답글 심사 결과를 기록한다", max_tokens=600)
    out["pass"] = bool(out.get("pass"))
    out["issues"] = list(out.get("issues") or [])
    out["confidence"] = float(out.get("confidence") or 0)
    out["by"] = {claude.HAIKU: "haiku", claude.SONNET: "sonnet", claude.OPUS: "opus"}.get(model, model)
    return out, usd


def review_two_stage(draft: str, facts: list[str], channel: str, ambiguous: float = 0.7,
                     question: str = "") -> tuple[dict, float]:
    """Haiku 심사 → 확신이 ambiguous 미만이면 Opus로 2차 심사(그 결과를 따른다).
    question(원글)을 같이 넘긴다 — 10-24 지식iN에서 원글 없이 심사해 '김포' 질문에 '김포'라는 단지를 답한 초안이 통과했다."""
    r1, c1 = review(draft, facts, channel, question=question)
    if r1.get("by") == "code" or r1.get("confidence", 0) >= ambiguous:
        return r1, c1
    r2, c2 = review(draft, facts, channel, model=claude.OPUS, question=question)
    r2["first"] = {"pass": r1.get("pass"), "confidence": r1.get("confidence")}
    return r2, c1 + c2


# ── 추출(Sonnet) — 글에서 조건을 구조화해 매칭을 좁힌다 ──────────────────────────
EXTRACT_SYSTEM = """너는 한국 부동산 커뮤니티 글에서 조건을 뽑는 추출기다. 글에 실제로 나온 것만, 없으면 빈 값.
- complexes: 아파트 단지명 후보. 줄임말·별칭이면 정식명 추정도 함께(예: '목동 7단지' → '목동신시가지7단지'). 최대 5개.
- sido·gu·dong: 시도(서울·경기 등)·시군구(양천구·성남시 분당구 등)·법정동(목동·신정동 등). 동네 이름(예: 목동)이 단지명 앞에 붙어 있으면 그 지역으로 본다.
  잘 알려진 동네는 글에 구 이름이 없어도 시도·구를 채운다(예: 목동→서울 양천구, 반포→서울 서초구, 대치·개포→서울 강남구, 상계→서울 노원구, 잠실→서울 송파구). 확신이 없으면 비운다.
- complexes에는 동네 접두를 뗀 이름도 함께 넣는다(예: '목동진도2차' → '목동진도2차', '진도2차').
- schools·stations: 학교명·역명(역 접미사 없이).
- area: 평형·면적대 표현 그대로(예: '30평대', '84㎡'). budget: 예산 표현 그대로(예: '10억 이하').
- purpose: 실거주·투자·기타 중 하나. concern: compare·price·school·jeonse·rebuild·commute·other 중 하나."""

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "complexes": {"type": "array", "items": {"type": "string"}},
        "sido": {"type": "string"}, "gu": {"type": "string"}, "dong": {"type": "string"},
        "schools": {"type": "array", "items": {"type": "string"}},
        "stations": {"type": "array", "items": {"type": "string"}},
        "area": {"type": "string"}, "budget": {"type": "string"},
        "purpose": {"type": "string", "enum": ["실거주", "투자", "기타"]},
        "concern": {"type": "string", "enum": ["compare", "price", "school", "jeonse", "rebuild", "commute", "other"]},
    },
    "required": ["complexes", "purpose", "concern"],
}


def extract(post_title: str, post_text: str) -> tuple[dict, float]:
    user = json.dumps({"title": (post_title or "")[:200], "text": (post_text or "")[:600]}, ensure_ascii=False)
    out, usd = claude.call_tool(claude.SONNET, EXTRACT_SYSTEM, user, "extract", EXTRACT_SCHEMA,
                                "글의 조건을 기록한다", max_tokens=500)
    out["complexes"] = [c for c in (out.get("complexes") or []) if isinstance(c, str) and c.strip()][:5]
    return out, usd


# ── 답변 작성(Sonnet) — facts 안의 숫자만 쓴다. 규칙 초안은 폴백으로 남는다 ──────────
DRAFT_SYSTEM = """너는 한국 부동산 커뮤니티에 답글을 쓰는 리블(reevl.co.kr) 운영자다. 리블은 전국 아파트 실거래·입지 데이터를 무료로 보여준다.
형식(2026-10-24 운영자 확정, 하나라도 어기면 안 된다):
- 정확히 두 줄. 괄호 금지. 날짜 금지. 이모지 금지.
- 1줄: 글쓴이가 물은 것(평형·단지·고민)에 facts의 수치로 바로 답한다. 글쓴이가 말한 평형이 있으면 '말씀하신 평형 기준'으로.
  예) 말씀하신 평형 기준 최근 실거래는 진도2 20평대 15억, 목동우성2 40평대 13.5억이에요.
- 2줄: '제가 운영하는 리블에 <무엇> 정리해 뒀어요 <link>' — 관계 공개가 이 문장 안에 들어간다. link는 받은 그대로 하나만.
- casual(디시·에펨·클리앙 등)은 음슴체: '…임.' / '내가 운영하는 리블에 <무엇> 정리해 둠 <link>'.
- video(유튜브)는 한 줄, 링크 없이 끝에 '제가 운영하는 리블에서 같은 조건으로 비교해 볼 수 있어요.'
- 숫자(금액·면적·평형)는 facts에 있는 표기 그대로만. 새 숫자·추정·평균 금지.
- 금지: 매수·매도 권유, 오른다·내린다 단정, 학교 배정 확정, 과장·비방."""

DRAFT_SCHEMA = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}


def draft(post: dict, entities: dict, facts: list[str], channel: str, link: str, style: str) -> tuple[dict, float]:
    user = json.dumps({"post": {"title": (post.get("title") or "")[:200], "text": (post.get("text") or "")[:600]},
                       "entities": entities or {}, "facts": facts, "link": link, "style": style, "channel": channel},
                      ensure_ascii=False)
    out, usd = claude.call_tool(claude.SONNET, DRAFT_SYSTEM, user, "answer", DRAFT_SCHEMA,
                                "답글 전문을 기록한다", max_tokens=500)
    out["text"] = (out.get("text") or "").strip()
    return out, usd
