"""게시글 → 댓글·답글 초안. 글에 나온 단지·학교·역·시공사를 리블 페이지로 잇고, 확인된 실거래만 근거로 쓴다.

- 첫 줄은 관계 공개, 끝 줄은 '참고용 통계' 고지. 링크는 최대 2개.
- AI 전망·순위·호가는 넣지 않는다. 빈 값·자리표시자가 남으면 실패로 돌려준다.
- 매칭이 없으면 ok=False — 링크 없는 답변을 권한다(틀린 단지를 링크하지 않는다).
- 게시는 pipeline이 허용된 채널에서만 하고, 실패하면 사람 대기열로 넘긴다.
"""
from __future__ import annotations

from datetime import date

from . import draft, hubs, match, reevl
from .links import complex_path, tracked

INTRO = "리블(reevl.co.kr)을 운영하고 있어 관계를 먼저 밝힙니다."
FOOT = "참고용 통계이며 매수·매도 권유가 아닙니다."


def _one(c: dict, today: date) -> tuple[list[str], int]:
    """단지 하나 — 대표 면적대 최근 실거래 1줄. 두 번째 값은 data 점수(3=같은 면적 최근 거래, 2=거래만)."""
    ts = reevl.trades(c["cid"])
    if not ts:
        return [f"- {c['name']}({c.get('dong', '')}): 확인된 매매 실거래가 없습니다."], 1
    band = draft.pick_band([ts], today)
    pool = [t for t in ts if band is not None and abs(t["area_m2"] - band) <= draft.BAND] or ts
    last = max(pool, key=lambda t: t["date"])
    n = len(draft._recent(pool, today))
    line = (f"- {c['name']}({c.get('dong', '')}, {c.get('hh', '?')}세대): 최근 {last['date']} {draft.won(last['price'])} "
            f"({last['floor']}층, 전용 {last['area_m2']}㎡) · 최근 1년 같은 면적 거래 {n}건")
    return [line], (3 if n >= 1 else 2)


def build(text: str, channel: str = "copy", placement: str | None = None, qa: bool = False,
          today: date | None = None) -> dict:
    today = today or date.today()
    try:
        cs = match.complexes(text)
    except Exception as e:                      # 공개 API 장애는 '매칭 없음'과 구분한다
        return {"ok": False, "why": f"단지 검색 실패: {str(e)[:120]}", "complexes": [], "hubs": [], "fit": 0, "data": 0}
    try:
        hs = hubs.find(text)
    except Exception:
        hs = []
    links, body, fit, data = [], [], 0, 0
    if len(cs) >= 2:
        d = draft.build([c["cid"] for c in cs], source=channel, placement=placement, today=today, qa=qa)
        body = [l for l in d["text"].splitlines()[1:] if not l.startswith("조건과 비교표")]
        links.append(("두 단지 비교표", d["link"]))
        fit, data = 3, (3 if d.get("band_m2") is not None else 2)
    elif len(cs) == 1:
        body, data = _one(cs[0], today)
        body.append(f"거래 시점·층에 따라 차이가 커서 한 건으로 시세를 단정하긴 어렵습니다. 조회 기준일 {today.isoformat()}, "
                    "원자료 국토교통부 실거래가.")
        links.append((f"{cs[0]['name']} 실거래·입지", tracked(complex_path(cs[0]["cid"]), channel, "complex", placement, qa)))
        fit = 2
    hub = next((h for h in hs if h["kind"] in ("midschool", "school", "highschool", "station")), hs[0] if hs else None)
    if hub:
        note = " (학교 주변 단지 목록이며 배정 확정은 관할 교육청에서 확인하세요)" if "school" in hub["kind"] else ""
        links.append((f"{hub['name']} {hub['label']} 주변 단지{note}", tracked(hub["path"], channel, "hub", placement, qa)))
        fit, data = fit or 1, data or 1
    if not links:
        return {"ok": False, "why": "글에서 리블 페이지와 확실히 이어지는 단지·학교·역을 찾지 못했습니다. 링크 없는 답변을 권합니다.",
                "complexes": cs, "hubs": hs, "fit": 0, "data": 0}
    links = links[:2]
    out = "\n".join([INTRO, *body, *[f"{name}: {url}" for name, url in links], FOOT])
    if "None" in out or "[" in out or "{" in out:
        return {"ok": False, "why": "초안에 빈 값·자리표시자가 남았습니다", "complexes": cs, "hubs": hs, "fit": 0, "data": 0}
    return {"ok": True, "text": out, "links": [u for _, u in links], "complexes": cs, "hubs": hs,
            "fit": fit, "data": data, "as_of": today.isoformat()}
