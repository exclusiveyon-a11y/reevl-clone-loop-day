"""게시글 → 짧은 댓글 초안. 글쓴이의 고민(의도)을 잡고, 확인된 실거래로 먼저 답한 뒤, 리블에서 그걸 푸는 방법을 링크 하나로.

형식(2~4줄, 커뮤니티 해요체):
  ①고민을 짚는 한 마디 + 데이터로 답(실거래 날짜·면적·가격)
  ②리블에서 되는 것 한 줄
  ③짧은 링크 하나 + 짧은 관계 공개("제가 만든 서비스라 참고만요")

- 관계 공개는 빼지 않는다(표시광고법 추천·보증 심사지침 — 운영자의 자기 서비스 홍보는 이해관계 공개 대상).
- 링크는 하나. 비교면 리블 단축 링크(/c/키), 단지 하나면 단지 주소, 단지가 없을 때만 학교·역 허브.
- AI 전망·순위·호가·'오른다' 단정 없음. 학교 배정 확정 표현 없음. 빈 값이 남으면 실패.
"""
from __future__ import annotations

import os
import urllib.parse
from datetime import date

from . import draft, hubs, intent, match, reevl, shortlink
from .links import SITE, complex_path, compare_path, tracked

DISCLOSE = os.environ.get("GTM_DISCLOSURE", "(제가 만든 서비스라 참고만 하세요)")


def eok(man: int) -> str:
    """만원 → '34.3억' · '8,900만'."""
    if man >= 10000:
        v = round(man / 10000, 1)
        return f"{v:g}억"
    return f"{man:,}만"


def md(d: str, today: date) -> str:
    """'2026-10-06' → '10/6' (올해) · '25.12' (지난해)."""
    y, m, dd = d.split("-")
    return f"{int(m)}/{int(dd)}" if int(y) == today.year else f"{y[2:]}.{int(m)}"


def _latest(cid: str, today: date, band: float | None = None) -> tuple[dict, int, float | None] | None:
    ts = reevl.trades(cid)
    if not ts:
        return None
    band = band if band is not None else draft.pick_band([ts], today)
    pool = [t for t in ts if band is not None and abs(t["area_m2"] - band) <= draft.BAND] or ts
    last = max(pool, key=lambda t: t["date"])
    return last, len(draft._recent(pool, today)), band


def _plain(path: str) -> str:
    return SITE + path


def build(text: str, channel: str = "copy", placement: str | None = None, qa: bool = False,
          today: date | None = None) -> dict:
    today = today or date.today()
    intents = intent.detect(text)
    try:
        cs = match.complexes(text)
    except Exception as e:
        return {"ok": False, "why": f"단지 검색 실패: {str(e)[:120]}", "complexes": [], "hubs": [], "fit": 0, "data": 0}
    try:
        hs = hubs.find(text)
    except Exception:
        hs = []

    lines, link, fit, data, kind = [], None, 0, 0, None
    if len(cs) >= 2:
        kind = "compare"
        cids = [c["cid"] for c in cs[:3]]
        tss = [reevl.trades(c) for c in cids]
        band = draft.pick_band(tss, today) if all(tss) else None
        parts = []
        for c, ts in zip(cs, tss):
            pool = [t for t in ts if band is not None and abs(t["area_m2"] - band) <= draft.BAND] or ts
            if pool:
                last = max(pool, key=lambda t: t["date"])
                parts.append(f"{c['name']} {eok(last['price'])}({md(last['date'], today)})")
        if len(parts) < 2:
            return {"ok": False, "why": "비교할 실거래가 부족합니다", "complexes": cs, "hubs": hs, "fit": 0, "data": 0}
        area = f"{round(band)}㎡ 기준 " if band is not None else ""
        lines.append(f"{intent.OPENER['compare']} {area}최근 실거래는 {', '.join(parts)}이에요. 층·시점 차이가 커서 한 건만 보시면 헷갈려요.")
        lines.append(f"{intent.SOLUTION['compare']}.")
        link = shortlink.compare(cids) or tracked(compare_path(cids), channel, "compare", placement, qa)
        fit, data = 3, (3 if band is not None else 2)
    elif len(cs) == 1:
        c = cs[0]
        got = _latest(c["cid"], today)
        if not got:
            return {"ok": False, "why": "실거래가 없습니다", "complexes": cs, "hubs": hs, "fit": 0, "data": 0}
        last, n, band = got
        want = next((i for i in intents if i in ("price", "jeonse", "rebuild", "school", "commute")), "price")
        kind = want
        lines.append(f"{intent.OPENER[want]} {c['name']} {round(last['area_m2'])}㎡ 최근 실거래가 "
                     f"{eok(last['price'])}({md(last['date'], today)}, {last['floor']}층)이고 최근 1년 같은 면적 거래가 {n}건이에요.")
        if want == "price" and __import__("re").search(r"고점|빠졌|떨어|하락", text):
            try:   # 고점을 물으면 고점 대비 위치로 답한다(값 그대로 인용, 단정 없음)
                hoff = (reevl.apartment(c["cid"]).get("가격") or {}).get("hoff")
                if isinstance(hoff, (int, float)):
                    lines[-1] += f" 역대 고점 대비 지금 대표가는 {hoff:g}% 수준이에요."
            except Exception:
                pass
        lines.append(f"{intent.SOLUTION[want]}.")
        link = _plain(complex_path(c["cid"]))
        fit, data = 2, (3 if n else 2)
    else:
        hub = next((h for h in hs if h["kind"] in ("midschool", "school", "highschool", "station")), None)
        if not hub:
            return {"ok": False, "why": "글에서 리블 페이지와 확실히 이어지는 단지·학교·역을 찾지 못했습니다",
                    "complexes": cs, "hubs": hs, "fit": 0, "data": 0}
        kind = "school" if "school" in hub["kind"] else "commute"
        lines.append(f"{intent.OPENER[kind]} {hub['name']} 기준으로 정리된 게 있어요.")
        lines.append(f"{intent.SOLUTION[kind]}.")
        link = _plain("/" + hub["kind"] + "/" + urllib.parse.quote(hub["name"]))
        fit, data = 1, 1

    st = intent.style(channel)
    if st == "video":
        # 유튜브: 링크 없이 짧게 — 서비스 이름과 관계 공개만
        out = f"{lines[0]} 리블(reevl)에서 같은 조건으로 비교해 볼 수 있어요(제가 만든 서비스예요)."
    else:
        disclose = "(내가 만든 사이트라 참고만)" if st == "casual" else DISCLOSE
        lines.append(f"{link} {disclose}")
        out = intent.restyle("\n".join(lines), channel)
    if "None" in out or "{" in out or "[" in out:
        return {"ok": False, "why": "초안에 빈 값이 남았습니다", "complexes": cs, "hubs": hs, "fit": 0, "data": 0}
    return {"ok": True, "text": out, "links": [link], "complexes": cs, "hubs": hs, "fit": fit, "data": data,
            "intents": intents, "kind": kind, "as_of": today.isoformat()}
