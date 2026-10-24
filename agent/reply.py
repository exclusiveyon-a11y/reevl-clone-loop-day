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
import re
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
    return f"{int(m)}/{int(dd)}" if int(y) == today.year else f"{y}년 {int(m)}월"


def _latest(cid: str, today: date, band: float | None = None) -> tuple[dict, int, float | None] | None:
    ts = reevl.trades(cid)
    if not ts:
        return None
    band = band if band is not None else draft.pick_band([ts], today)
    pool = [t for t in ts if band is not None and abs(t["area_m2"] - band) <= draft.BAND] or ts
    last = max(pool, key=lambda t: t["date"])
    return last, len(draft._recent(pool, today)), band


def _fact(name: str, t: dict, today: date) -> str:
    """심사(judge.review)가 대조할 사실 한 줄 — 초안에 쓰는 금액·날짜 표기를 그대로 담는다."""
    return (f"{name} 전용 {t['area_m2']}㎡({round(t['area_m2'])}㎡) {eok(t['price'])} "
            f"거래일 {t['date']}({md(t['date'], today)}) {t['floor']}층")


def _plain(path: str) -> str:
    return SITE + path


def _pyeong(entities: dict | None, text: str) -> list[int]:
    """글쓴이가 말한 평형대(20·30·40평대 …)를 나온 순서대로. 예: '진도2차 20평대 와 우성2차 40평대' → [20, 40]."""
    for src in (text or "", str((entities or {}).get("area") or "")):   # 본문 우선, 없으면 Claude 추출값
        got = [int(m) * 10 for m in re.findall(r"([1-9])0\s*평대", src)]
        if got:
            return got[:3]
    return []


def _in_pyeong(ts: list[dict], py: int) -> list[dict]:
    """N평대 → 전용면적 범위(대략 N×2.5 ~ (N+10)×2.6㎡)에서 가장 많이 거래된 면적의 거래들."""
    lo, hi = py * 2.5, (py + 10) * 2.6
    cand = [t for t in ts if lo <= t["area_m2"] < hi]
    if not cand:
        return []
    from collections import Counter
    top = Counter(round(t["area_m2"]) for t in cand).most_common(1)[0][0]
    return [t for t in cand if abs(t["area_m2"] - top) <= 1.5]


def build(text: str, channel: str = "copy", placement: str | None = None, qa: bool = False,
          today: date | None = None, entities: dict | None = None) -> dict:
    today = today or date.today()
    intents = intent.detect(text)
    if entities and entities.get("concern") and entities["concern"] != "other" and entities["concern"] not in intents:
        intents = [entities["concern"]] + intents          # Claude가 잡은 고민 유형을 앞에
    cs, via = [], "rule"
    if entities and entities.get("complexes"):
        try:
            cs = match.complexes_from_entities(entities)
            via = "entities" if cs else "rule"
        except Exception:
            cs = []
    if not cs and entities and entities.get("complexes"):
        # ★Claude가 단지를 뽑았는데 리블에서 확정하지 못하면 규칙 매칭으로 돌아가지 않는다 —
        #   규칙 매칭이 '목동 진도2차'를 금천구 독산동 진도2차로 잡아 틀린 실거래를 쓴 적이 있다(2026-10-24).
        return {"ok": False, "why": "추출한 단지를 리블에서 확정하지 못함(동명 단지·표기 차이) — 틀린 단지를 쓰지 않으려 제외",
                "complexes": [], "hubs": [], "fit": 0, "data": 0, "match_via": "entities_none"}
    if not cs:
        try:
            cs = match.complexes(text)
        except Exception as e:
            return {"ok": False, "why": f"단지 검색 실패: {str(e)[:120]}", "complexes": [], "hubs": [], "fit": 0, "data": 0}
    try:
        hs = hubs.find(text)
    except Exception:
        hs = []

    lines, link, fit, data, kind, facts = [], None, 0, 0, None, []
    # ★형식(2026-10-24 사용자 확정): 두 줄 · 괄호 없음 · 날짜 없음 · 글쓴이가 말한 평형 기준 · 공개는 문장 안에
    #   1줄 = 데이터로 답, 2줄 = "제가 운영하는 리블에 … 정리해 뒀어요 <링크>"
    pyeong = _pyeong(entities, text)
    if len(cs) >= 2:
        kind = "compare"
        cids = [c["cid"] for c in cs[:3]]
        tss = [reevl.trades(c) for c in cids]
        parts, band_all = [], None
        if len(pyeong) == len(cids):               # 단지마다 말한 평형이 있으면 그 평형대에서 가장 많이 거래된 면적
            for c, ts, py in zip(cs, tss, pyeong):
                pool = _in_pyeong(ts, py)
                if not pool:
                    parts = []
                    break
                last = max(pool, key=lambda t: t["date"])
                parts.append(f"{c['name']} {py}평대 {eok(last['price'])}")
                facts.append(_fact(c['name'], last, today))
            head = "말씀하신 평형 기준 최근 실거래는"
        if not parts:
            facts = []
            band_all = draft.pick_band(tss, today) if all(tss) else None
            for c, ts in zip(cs, tss):
                pool = [t for t in ts if band_all is not None and abs(t["area_m2"] - band_all) <= draft.BAND] or ts
                if pool:
                    last = max(pool, key=lambda t: t["date"])
                    parts.append(f"{c['name']} {eok(last['price'])}")
                    facts.append(_fact(c['name'], last, today))
            head = f"같은 {round(band_all)}㎡ 기준 최근 실거래는" if band_all is not None else "최근 실거래는"
        if len(parts) < 2:
            return {"ok": False, "why": "비교할 실거래가 부족합니다", "complexes": cs, "hubs": hs, "fit": 0, "data": 0}
        lines.append(f"{head} {', '.join(parts)}이에요.")
        link = shortlink.compare(cids) or tracked(compare_path(cids), channel, "compare", placement, qa)
        tail = "두 단지 평형별 실거래 비교"
        fit, data = 3, 3
    elif len(cs) == 1:
        c = cs[0]
        ts = reevl.trades(c["cid"])
        pool = _in_pyeong(ts, pyeong[0]) if pyeong else []
        label = f"{pyeong[0]}평대" if pool else ""
        if not pool:
            got = _latest(c["cid"], today)
            if not got:
                return {"ok": False, "why": "실거래가 없습니다", "complexes": cs, "hubs": hs, "fit": 0, "data": 0}
            last, n, band = got
            label = f"{round(last['area_m2'])}㎡"
        else:
            last = max(pool, key=lambda t: t["date"])
        facts.append(_fact(c['name'], last, today))
        want = next((i for i in intents if i in ("price", "jeonse", "rebuild", "school", "commute")), "price")
        kind = want
        line = f"{c['name']} {label} 최근 실거래는 {eok(last['price'])}이에요."
        if want == "price" and re.search(r"고점|빠졌|떨어|하락", text):
            try:   # 고점을 물으면 고점 대비 위치로 답한다(값 그대로 인용, 단정 없음)
                hoff = (reevl.apartment(c["cid"]).get("가격") or {}).get("hoff")
                if isinstance(hoff, (int, float)):
                    line += f" 역대 고점 대비 지금 {hoff:g}% 수준이에요."
                    facts.append(f"{c['name']} 역대 고점 대비 현재 대표가 {hoff:g}%")
            except Exception:
                pass
        lines.append(line)
        link = _plain(complex_path(c["cid"]))
        tail = {"price": "평형별 실거래 흐름", "jeonse": "매매·전세 실거래와 전세가율", "rebuild": "연식·세대수·실거래 흐름",
                "school": "주변 학교와 실거래", "commute": "역까지 거리와 실거래"}[want]
        fit, data = 2, 3
    else:
        hub = next((h for h in hs if h["kind"] in ("midschool", "school", "highschool", "station")), None)
        if not hub:
            return {"ok": False, "why": "글에서 리블 페이지와 확실히 이어지는 단지·학교·역을 찾지 못했습니다",
                    "complexes": cs, "hubs": hs, "fit": 0, "data": 0}
        kind = "school" if "school" in hub["kind"] else "commute"
        lines.append(f"{hub['name']} 주변 단지 실거래를 한 페이지에 모아 볼 수 있어요." +
                     (" 배정은 교육청 확인이 필요해요." if kind == "school" else ""))
        link = _plain("/" + hub["kind"] + "/" + urllib.parse.quote(hub["name"]))
        facts.append(f"{hub['name']} {hub.get('label', '')} 주변 단지 목록 페이지(리블)")
        tail = f"{hub['name']} 주변 단지"
        fit, data = 1, 1

    st = intent.style(channel)
    if st == "video":
        out = f"{lines[0]} 제가 운영하는 리블에서 같은 조건으로 비교해 볼 수 있어요."   # 유튜브: 링크 없이
    elif st == "casual":
        out = f"{lines[0]}\n내가 운영하는 리블에 {tail} 정리해 둠 {link}"
        out = intent.restyle(out, channel)
    else:
        out = f"{lines[0]}\n제가 운영하는 리블에 {tail} 정리해 뒀어요 {link}"
    if "None" in out or "{" in out or "[" in out:
        return {"ok": False, "why": "초안에 빈 값이 남았습니다", "complexes": cs, "hubs": hs, "fit": 0, "data": 0}
    return {"ok": True, "text": out, "links": [link], "complexes": cs, "hubs": hs, "fit": fit, "data": data,
            "facts": facts, "match_via": via,
            "intents": intents, "kind": kind, "as_of": today.isoformat()}
