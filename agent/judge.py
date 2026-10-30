"""Claude 판정(triage)과 발행 전 심사(review) — 사람 검수를 줄이는 두 관문.

- triage: 실제 주거 질문인가 / 리블 페이지가 답이 되는가 / 댓글이 적절한가 + 조건 구조화. Haiku.
- review: 코드가 먼저 숫자·날짜·링크·공개 문구를 대조하고(실패면 Claude를 부르지 않는다), 통과하면 Haiku가 표현을 심사.
- Claude는 숫자를 만들지 않는다. 숫자는 리블 공개 API에서 코드가 가져온 facts 안에서만.
- 판정(triage)은 검색 요약, 추출·작성·검수는 원문(reader)을 쓴다. 작성자·댓글 닉네임은 넣지 않는다.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

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
_UNITNUM = re.compile(r"[+-]?\d[\d,]*(?:\.\d+)?\s?(?:세대|년|점|%)")
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
    blob_n = re.sub(r"\s+", "", blob)
    for m in _UNITNUM.finditer(body):
        tok = re.sub(r"\s+", "", m.group(0))
        if tok not in blob_n and not any(tok.lstrip("+-") == t.lstrip("+-") for t in re.findall(r"[+-]?\d[\d,]*(?:\.\d+)?(?:세대|년|점|%)", blob_n)):
            issues.append(f"facts에 없는 수치: {m.group(0)}")
    if len(_URL.findall(draft)) > 1:
        issues.append("링크가 2개 이상")
    if not _DISCLOSE.search(draft):
        issues.append("관계 공개 누락")
    return issues


_EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")


def format_check(draft: str, style: str = "polite") -> list[str]:
    """형식(2026-10-24 사용자 확정) — 두 줄·괄호 없음·이모지 없음. 유튜브(video)는 한 줄."""
    issues = []
    lines = [ln for ln in draft.splitlines() if ln.strip()]
    want = 1 if style == "video" else 2
    if len(lines) != want:
        issues.append(f"{want}줄이 아님({len(lines)}줄)")
    if re.search(r"[()（）]", _URL.sub(" ", draft)):
        issues.append("괄호가 있음")
    if _EMOJI.search(draft):
        issues.append("이모지가 있음")
    return issues


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def link_check(link: str, cids: list[str]) -> list[str]:
    """링크가 답글이 다룬 단지를 여는가 — 단축 링크(/c/키)는 이동 주소의 compare=에 단지 cid가 모두 있는지 본다."""
    if not link:
        return ["링크 없음"]
    if "/c/" not in link:
        path = urllib.parse.unquote(link)
        return [] if all(c in path for c in cids) else ["링크가 답글의 단지 페이지가 아님"]
    try:
        urllib.request.build_opener(_NoRedirect).open(urllib.request.Request(link, method="GET",
                                                      headers={"User-Agent": "reevl-gtm-agent/0.1"}), timeout=10)
        return ["단축 링크가 이동하지 않음"]
    except urllib.error.HTTPError as e:
        loc = urllib.parse.unquote(e.headers.get("Location") or "")
        if e.code not in (301, 302, 307, 308) or not loc:
            return [f"단축 링크 응답 {e.code}"]
        miss = [c.split("|")[1] for c in cids if c not in loc]
        return [f"단축 링크에 없는 단지: {', '.join(miss)}"] if miss else []
    except Exception as e:
        return [f"단축 링크 확인 실패: {type(e).__name__}"]


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


# ── 최종 검수(audit) — 원문 전체·기존 댓글·단지 주소를 보고 Opus·Sonnet 두 모델이 따로 판정, 둘 다 통과해야 통과 ──
# 10-24까지의 review는 검색 요약(약 200자)만 보고 Haiku가 먼저 판정했다. 지식iN 첫 실행에서 통과 5건 중 4건이 엉뚱한 단지였고
# ('서촌 한옥'→인천 하나아파트, 남양주 덕소→수원 화서주공), 심사가 답글 단지의 실제 주소를 몰라 잡지 못했다.
AUDIT_CHECKS = ["live_question", "right_target", "answers_ask", "facts_only", "no_advice",
                "honest_tone", "not_redundant", "fits_board", "link_fit"]

AUDIT_SYSTEM = """너는 부동산 커뮤니티 답글의 최종 검수자다. 통과하면 사람이 문장을 다시 고치지 않고 그대로 올라간다. 확실하지 않으면 떨어뜨린다.
입력: post(게시판·제목·본문 전체·작성 시각), comments(이미 달린 댓글 본문), draft(올릴 답글), facts(답글 수치의 출처 — 리블 공개 실거래),
complexes(답글이 다룬 단지의 리블 기준 이름·시군구·동·세대수), link(답글 링크가 여는 페이지 설명).

아홉 항목을 서로 독립적으로 판정해 checks에 ok와 한 줄 근거를 적는다.
1 live_question: 글쓴이가 지금 주거 판단(매매·전세·실거주·학군·통근·재건축·청약)을 위해 묻는 글인가. 광고·분양 홍보·뉴스 퍼옴·후기·공지·잡담, 글쓴이가 이미 결론을 냈다고 밝힌 글이면 false.
2 right_target: 답글의 단지 하나하나가 글쓴이가 말한 바로 그 단지인가. complexes의 시군구·동을 글의 지역 단서와 대조한다. 이름이 비슷해도 지역이 다르면 false(예: 남양주 덕소 질문에 수원 화서주공). 글에 지역 단서가 없는데 흔한 이름이면 false. 글쓴이가 단지를 말하지 않았는데 답글이 임의 단지를 골랐으면 false.
3 answers_ask: 글쓴이의 핵심 질문 가운데 적어도 하나에 사실로 직접 답하는가. 전부 답할 필요는 없다. 비교 질문이면 글쓴이가 비교한 단지를 모두 다뤄야 한다. 답하지 않은 부분(교육·전망 등)을 답한 척하면 false. 글쓴이의 전제(예: 두 단지 가격이 같다)와 facts가 다르면 facts대로 쓴 답은 도움이 되는 정보로 본다. 글쓴이의 고민이 예산·대출·청약 전략처럼 실거래 수치로 답할 수 없는 것이면 false.
4 facts_only: 답글의 금액·면적·평형·단지명이 facts에 있는 그대로인가. facts에 함께 적힌 반올림 표기(84.8㎡ → 85㎡)는 허용.
5 no_advice: 매수·매도 권유, 오른다·내린다 단정이나 전망, 학교 배정 확정, 세무·법률·대출 판단이 없는가.
6 honest_tone: '제가 운영하는'류 관계 공개가 있고, 정보가 먼저이며, 홍보 문구·과장·다른 서비스 비하가 없고, 링크가 하나인가.
7 not_redundant: 기존 댓글에 같은 수치나 리블 링크가 이미 있지 않은가. 있으면 false.
8 fits_board: 게시판이 질문·상담에 맞는가. 공지·광고·분양홍보·매물·후기·출석·가입인사 게시판이면 false. 게시판 이름이 비어 있으면 true.
9 link_fit: link가 답글이 다룬 단지(들)의 페이지인가.

★kind가 explore면 지역 조회형이다(2026-10-30): 글쓴이가 말한 지역의 단지 목록에서 질문에 맞는 단지를 골라 리블 대표가·연식·세대수로 답한다.
  - right_target: 고른 단지가 글에서 말한 지역에 있으면 ok. answers_ask: 질문(신축·구축·가격 차이·규모)에 닿는 사실이 있으면 ok. 가격이 실거래가가 아니라 '리블 대표가'로 밝혀져 있어야 한다.
  - no_advice는 엄격하다(원인 단정·추천·전망 금지).
★kind가 link 또는 hub면 링크형 답글이다(2026-10-30 운영자 확정): 가격을 비교하지 않고, 글이 말한 단지·학교·역·지역의 리블 페이지로 잇는다.
  - answers_ask: 글쓴이 질문과 관련된 사실을 하나 이상 주면 ok. 비교 질문에 일부 단지만 다뤄도 ok(다룬 단지가 맞는 단지이면). 질문과 무관한 사실만 있으면 false.
  - right_target: 링크 단지·허브가 글에 실제로 나온 대상이면 ok.
  - link_fit: 링크가 단지 페이지·허브 페이지이고 답글이 말한 대상이면 ok.
  - no_advice는 그대로 엄격하다(전망·추천·단정 금지). 리블 점수는 리블이 계산한 값으로 밝혀야 한다.

pass는 아홉 항목이 모두 ok일 때만 true. 근거는 한국어로 짧게. confidence는 전체 판정 확신(0~1)."""

AUDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "checks": {"type": "object", "properties": {
            k: {"type": "object", "properties": {"ok": {"type": "boolean"}, "why": {"type": "string"}}, "required": ["ok", "why"]}
            for k in AUDIT_CHECKS}, "required": AUDIT_CHECKS},
        "pass": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["checks", "pass", "confidence"],
}


def _audit_one(model: str, payload: str) -> tuple[dict, float]:
    # ★max_tokens는 넉넉히 — Sonnet 5.5는 생각 토큰을 먼저 쓴다. 1,200으로 두었더니 판정 JSON이 잘려 맞는 답글을 떨어뜨렸다(10-25 평가)
    usd = 0.0
    for _ in range(2):
        out, u = claude.call_tool(model, AUDIT_SYSTEM, payload, "audit", AUDIT_SCHEMA, "검수 결과를 기록한다", max_tokens=4000)
        usd += u
        checks = out.get("checks") or {}
        if isinstance(checks, dict) and all(isinstance(checks.get(k), dict) and "ok" in checks[k] for k in AUDIT_CHECKS):
            break                                # 아홉 항목이 다 있으면 끝, 빠졌으면 한 번 더 묻는다
    checks = out.get("checks") or {}
    if not isinstance(checks, dict):
        checks = {}
    failed = [k for k in AUDIT_CHECKS if not (checks.get(k) or {}).get("ok")]
    return {"pass": bool(out.get("pass")) and not failed, "failed": failed,
            "why": {k: ((checks.get(k) or {}).get("why") or "검수 응답에 이 항목이 없음")[:160] for k in failed},
            "confidence": float(out.get("confidence") or 0)}, usd


def audit(post: dict, comments: list[str], draft: str, facts: list[str], complexes: list[dict], link: str,
          channel: str, style: str = "polite", kind: str = "price") -> tuple[dict, float]:
    """코드 검사(수치·날짜·공개·형식·링크 대상) → Opus·Sonnet 독립 판정. 둘 다 통과해야 pass.
    post = {board, title, text, written}. 원문은 3,000자, 댓글은 15개까지. 닉네임은 넣지 않는다."""
    cids = [c.get("cid", "") for c in complexes if c.get("cid")]
    issues = code_check(draft, facts) + format_check(draft, style) + link_check(link, cids)
    if issues:                                   # 코드에서 걸리면 Claude를 부르지 않는다
        return {"pass": False, "issues": issues, "confidence": 1.0, "by": "code", "failed": ["code"]}, 0.0
    payload = json.dumps({
        "channel": channel,
        "post": {"board": post.get("board", ""), "title": (post.get("title") or "")[:200],
                 "text": (post.get("text") or "")[:3000], "written": post.get("written", "")},
        "comments": [c[:200] for c in (comments or [])[:15]],
        "draft": draft, "facts": facts, "kind": kind,
        "complexes": [{"name": c.get("name"), "sigungu": c.get("gu"), "dong": c.get("dong"), "households": c.get("hh")}
                      for c in complexes],
        "link": ("리블 비교 페이지: " if "/c/" in link else "리블 단지 페이지: ") + ", ".join(c.get("name", "") for c in complexes),
    }, ensure_ascii=False)
    total, votes = 0.0, {}
    for name, model in (("opus", claude.OPUS), ("sonnet", claude.SONNET)):
        v, usd = _audit_one(model, payload)
        total += usd
        votes[name] = v
        if not v["pass"]:                        # 한쪽이 떨어뜨리면 다른 쪽은 부르지 않는다(비용)
            break
    ok = len(votes) == 2 and all(v["pass"] for v in votes.values())
    failed = sorted({k for v in votes.values() for k in v["failed"]})
    issues = [f"{k}: {v['why'][k]}" for v in votes.values() for k in v["failed"]]
    return {"pass": ok, "issues": issues, "failed": failed, "votes": votes,
            "confidence": min(v["confidence"] for v in votes.values()), "by": "+".join(votes)}, total


# ── 추출(Sonnet) — 글에서 조건을 구조화해 매칭을 좁힌다 ──────────────────────────
EXTRACT_SYSTEM = """너는 한국 부동산 커뮤니티 글에서 조건을 뽑는 추출기다. 글에 실제로 나온 것만, 없으면 빈 값.
- complexes: 아파트 단지명 후보. 줄임말·별칭이면 정식명 추정도 함께(예: '목동 7단지' → '목동신시가지7단지'). 최대 5개.
- sido·gu·dong: 시도(서울·경기 등)·시군구(양천구·성남시 분당구 등)·법정동(목동·신정동 등). 동네 이름(예: 목동)이 단지명 앞에 붙어 있으면 그 지역으로 본다.
  잘 알려진 동네는 글에 구 이름이 없어도 시도·구를 채운다(예: 목동→서울 양천구, 반포→서울 서초구, 대치·개포→서울 강남구, 상계→서울 노원구, 잠실→서울 송파구). 확신이 없으면 비운다.
- complexes에는 동네 접두를 뗀 이름도 함께 넣는다(예: '목동진도2차' → '목동진도2차', '진도2차').
- schools·stations: 학교명·역명(역 접미사 없이).
- area: 평형·면적대 표현 그대로(예: '30평대', '84㎡'). budget: 예산 표현 그대로(예: '10억 이하').
- purpose: 실거주·투자·기타 중 하나. concern: compare·price·school·jeonse·rebuild·commute·other 중 하나.
- targets: 글쓴이가 고르거나 묻는 단지마다 하나씩(최대 3). 예시로 스친 단지·참고로 든 단지·구역 번호(한남5구역 등 아직 단지가 아닌 것)는 넣지 않는다.
  name=글에 쓴 표기 그대로, aliases=정식명 추정·동네 뗀 이름·줄임말 풀이(최대 3), sido·gu·dong=그 단지의 지역.
  단지마다 지역이 다르면 각각 채운다(예: '분당하이스트 vs 테헤란아이파크' → 성남시 분당구 / 강남구). 모르면 비운다."""

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "complexes": {"type": "array", "items": {"type": "string"}},
        "targets": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "aliases": {"type": "array", "items": {"type": "string"}},
            "sido": {"type": "string"}, "gu": {"type": "string"}, "dong": {"type": "string"}}, "required": ["name"]}},
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
    user = json.dumps({"title": (post_title or "")[:200], "text": (post_text or "")[:2000]}, ensure_ascii=False)
    out, usd = claude.call_tool(claude.SONNET, EXTRACT_SYSTEM, user, "extract", EXTRACT_SCHEMA,
                                "글의 조건을 기록한다", max_tokens=3000)
    out["complexes"] = [c for c in (out.get("complexes") or []) if isinstance(c, str) and c.strip()][:5]
    out["targets"] = [t for t in (out.get("targets") or []) if isinstance(t, dict) and (t.get("name") or "").strip()][:3]
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
- kind가 explore면(지역 조회형): facts의 단지만 쓰고, 글쓴이 질문(신축·구축·가격 차이·규모 등)에 닿는 대비(연식·세대수·대표가)를 한 문장으로 쓴다.
  가격은 반드시 '리블 대표가'라고 쓴다(실거래가가 아니다). 비교하는 면적이 다르면 그 사실을 밝힌다. 원인을 단정하지 않는다(예: '연식과 세대수가 다르다'까지만).
- kind가 link/hub면(링크형): 가격 비교로 답하지 않는다. facts 중 글쓴이 질문과 가장 관련 있는 사실 1~2개만 쓴다(학군이면 학군 점수, 통근이면 역세권 점수, 관리비면 월 관리비, 규모·연식이면 세대수·준공 후 연수).
  점수는 '리블 학군 점수 60점'처럼 리블 점수임을 밝히고, 단지 이름은 facts 표기 그대로. 질문과 직접 관련된 사실이 facts에 없으면 규모(세대수)와 연식만 쓴다. 링크는 받은 그대로.
- 금지: 매수·매도 권유, 오른다·내린다 단정, 학교 배정 확정, 과장·비방."""

DRAFT_SCHEMA = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}


def draft(post: dict, entities: dict, facts: list[str], channel: str, link: str, style: str, kind: str = "price") -> tuple[dict, float]:
    user = json.dumps({"post": {"title": (post.get("title") or "")[:200], "text": (post.get("text") or "")[:1500]},
                       "entities": entities or {}, "facts": facts, "link": link, "style": style, "channel": channel, "kind": kind},
                      ensure_ascii=False)
    out, usd = claude.call_tool(claude.SONNET, DRAFT_SYSTEM, user, "answer", DRAFT_SCHEMA,
                                "답글 전문을 기록한다", max_tokens=3000)
    out["text"] = (out.get("text") or "").strip()
    return out, usd


# ── 질문 적합성 관문(10-30) ─────────────────────────────────────────────
# 게시한 10건을 Opus로 사후 채점했더니 '질문 적합성'이 평균 2.0/5였다 — 검수(audit)는 통과했지만 '어디가 나은지'에는 닿지 못하고
# 실거래 두 줄만 적은 답이 대부분이었다. 그래서 같은 채점표를 게시 전 관문으로 쓴다: 질문 적합성 3 미만이면 게시 후보에서 뺀다.
GRADE_ITEMS = ["relevance", "evidence", "accuracy", "non_promo", "natural", "followup"]
GRADE_SYSTEM = """너는 부동산 커뮤니티 답글의 평가자다. 운영자가 올릴 답글(draft)을 원글(post)과 기존 댓글(others)에 비추어 1~5점으로 채점한다(5가 좋음).
항목: relevance(질문 적합성: 글쓴이가 실제로 물은 것에 닿았는가 — 고민이 '어디가 나은지·왜 비싼지·학군이 어떤지'인데 가격만 나열했다면 2점 이하) ·
evidence(실거래 근거: 수치가 출처 있는 사실로 제시됐는가) · accuracy(정확성: 단지·평형·수치가 글과 맞는가, 과장·단정이 없는가) ·
non_promo(비광고성: 정보가 먼저이고 링크가 부가적이며 관계 공개가 있는가) · natural(자연스러움) · followup(후속 대응 가능성).
엄격하게 채점한다. 각 점수 옆 *_why에 한 줄 근거, weakness에 가장 큰 약점 한 문장. 입력에 없는 사실을 지어내지 않는다."""
GRADE_SCHEMA = {"type": "object",
                "properties": {**{k: {"type": "integer", "minimum": 1, "maximum": 5} for k in GRADE_ITEMS},
                               **{k + "_why": {"type": "string"} for k in GRADE_ITEMS}, "weakness": {"type": "string"}},
                "required": GRADE_ITEMS + [k + "_why" for k in GRADE_ITEMS] + ["weakness"]}
MIN_RELEVANCE = int(os.environ.get("GTM_MIN_RELEVANCE", "3"))


def grade(post: dict, comments: list[str], draft: str, model: str = claude.OPUS) -> tuple[dict, float]:
    payload = json.dumps({"post": {"title": (post.get("title") or "")[:200], "text": (post.get("text") or "")[:1800], "board": post.get("board", "")},
                          "others": [c[:160] for c in (comments or []) if "reevl" not in c][:12], "draft": draft}, ensure_ascii=False)
    total = 0.0
    for _ in range(3):
        v, usd = claude.call_tool(model, GRADE_SYSTEM, payload, "grade", GRADE_SCHEMA, "채점 결과를 기록한다", max_tokens=4000)
        total += usd
        if all(isinstance(v.get(k), int) and 1 <= v[k] <= 5 for k in GRADE_ITEMS):
            return v, total
    return {}, total


def relevance_gate(post: dict, comments: list[str], draft: str) -> tuple[bool, dict, float]:
    """(통과 여부, 채점, 비용). 채점이 안 되면 통과시키지 않는다."""
    v, usd = grade(post, comments, draft)
    return bool(v) and v["relevance"] >= MIN_RELEVANCE, v, usd


# ── 지역 조회형(explore) 단지 고르기(10-30) ───────────────────────────────
PICK_SYSTEM = """너는 부동산 커뮤니티 글에 답할 때 '글쓴이가 실제로 궁금해하는 단지'를 후보 목록에서 고르는 도우미다.
입력: post(제목·본문), candidates(그 지역 단지: name·세대수 hh·준공 후 연수 age·대표가 price(만원)·대표 전용면적 area).
- 글이 단지를 직접 말했으면 그 단지를, 신축·구축·평형·가격차를 묻는 글이면 질문에 가장 잘 맞는 대비 쌍(예: 신축 1곳 + 구축 1곳)을 2~3개 고른다.
- 글에 없는 조건을 만들지 않는다. 후보 목록에 없는 이름은 고르지 않는다. 고를 근거가 없으면 빈 배열을 돌려준다.
- 반드시 candidates의 cid를 그대로 돌려준다."""
PICK_SCHEMA = {"type": "object", "properties": {"cids": {"type": "array", "items": {"type": "string"}}, "reason": {"type": "string"}}, "required": ["cids", "reason"]}


def pick(post: dict, candidates: list[dict]) -> tuple[list[str], float]:
    payload = json.dumps({"post": {"title": (post.get("title") or "")[:200], "text": (post.get("text") or "")[:1200]},
                          "candidates": [{k: c.get(k) for k in ("cid", "name", "hh", "age", "price", "area")} for c in candidates[:40]]}, ensure_ascii=False)
    out, usd = claude.call_tool(claude.SONNET, PICK_SYSTEM, payload, "pick", PICK_SCHEMA, "고른 단지를 기록한다", max_tokens=3000)
    valid = {c["cid"] for c in candidates}
    cids = [c for c in (out.get("cids") or []) if c in valid]
    return list(dict.fromkeys(cids))[:3], usd
