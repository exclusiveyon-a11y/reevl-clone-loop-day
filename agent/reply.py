"""게시글 → 짧은 댓글 초안. 글쓴이의 고민(의도)을 잡고, 확인된 실거래로 먼저 답한 뒤, 리블에서 그걸 푸는 방법을 링크 하나로.

형식(두 줄, 2026-10-24 사용자 확정):
  ①글쓴이가 말한 평형 기준 실거래로 답(괄호·날짜 없음)
  ②"제가 운영하는 리블에 … 정리해 뒀어요 <링크>" — 관계 공개를 문장 안에

- 관계 공개는 빼지 않는다(표시광고법 추천·보증 심사지침 — 운영자의 자기 서비스 홍보는 이해관계 공개 대상).
- 링크는 하나. 비교면 리블 단축 링크(/c/키), 단지 하나면 단지 주소, 단지가 없을 때만 학교·역 허브.
- AI 전망·순위·호가·'오른다' 단정 없음. 학교 배정 확정 표현 없음. 빈 값이 남으면 실패.
"""
from __future__ import annotations

import os
import re
import urllib.parse
from datetime import date

from . import draft, hubs, intent, judge, match, reevl, shortlink
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
    """글쓴이가 말한 평형을 나온 순서대로. 평형대는 10의 배수, 정확한 평수는 음수로 표시한다.
    예: '진도2차 20평대 와 우성2차 40평대' → [20, 40] · '수지삼성1차 (49평) vs 만현마을5단지 (37평)' → [-49, -37]
    ★제목과 본문에 같은 평수가 되풀이되면(10-26 수지삼성1차) 단지 수와 어긋난다 — 제목 → 본문 → 추출값 순서로 처음 걸린 곳만 쓴다."""
    title, _, body = (text or "").partition("\n")
    for src in (title, body, str((entities or {}).get("area") or "")):
        got = [int(m) * 10 for m in re.findall(r"([1-9])0\s*평대", src)]
        if got:
            return got[:3]
        got = [-int(m) for m in re.findall(r"(?<![\d.])([1-7]\d)\s*평(?!대)", src)]
        if got:
            return got[:3]
    return []


def _plabel(py: int) -> str:
    return f"{-py}평" if py < 0 else f"{py}평대"


def _in_pyeong(ts: list[dict], py: int) -> list[dict]:
    """N평대 → 전용면적 범위(대략 N×2.5 ~ (N+10)×2.6㎡), 정확한 N평(공급 평수) → 전용 N×2.3 ~ N×2.9㎡.
    그 범위에서 가장 많이 거래된 면적의 거래들."""
    lo, hi = (-py * 2.3, -py * 2.9) if py < 0 else (py * 2.5, (py + 10) * 2.6)
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
            cs = match.complexes_from_entities(entities, text=text)
            via = "entities" if cs else "rule"
        except Exception:
            cs = []
    if not cs and entities and entities.get("complexes"):
        # ★Claude가 단지를 뽑았는데 리블에서 확정하지 못하면 규칙 매칭으로 돌아가지 않는다 —
        #   규칙 매칭이 '목동 진도2차'를 금천구 독산동 진도2차로 잡아 틀린 실거래를 쓴 적이 있다(2026-10-24).
        return {"ok": False, "why": "추출한 단지를 리블에서 확정하지 못함(동명 단지·표기 차이) — 틀린 단지를 쓰지 않으려 제외",
                "complexes": [], "hubs": [], "fit": 0, "data": 0, "match_via": "entities_none"}
    tg = (entities or {}).get("targets") or []
    if cs and entities and entities.get("concern") == "compare" and len(tg) >= 2 and len(cs) < min(len(tg), 3):
        # 셋을 묻는데 둘만 답하면 검수가 떨어뜨린다(10-25 '세 단지 중 두 곳만 답함') — 초안 전에 뺀다
        return {"ok": False, "why": f"비교 대상 {len(tg)}곳 중 {len(cs)}곳만 확정", "complexes": cs, "hubs": [], "fit": 0, "data": 0,
                "match_via": "entities_half"}
    if len(cs) == 1 and entities and entities.get("concern") == "compare":
        # 비교 질문에 한 단지만 답하면 반쪽 답이다(10-24 '부평현대2차 vs 부개주공3단지'에 현대2차만 답한 초안이 심사를 통과)
        return {"ok": False, "why": "비교 질문인데 두 단지를 모두 확정하지 못함", "complexes": cs, "hubs": [], "fit": 0, "data": 0,
                "match_via": "entities_half"}
    if not cs and entities is None:
        # 규칙 매칭은 Claude 없이 돌 때만 쓴다. Claude가 읽고도 단지를 못 뽑은 글에서 규칙 매칭은
        # '서촌 한옥'→인천 하나아파트, '김포'→'김포'라는 단지처럼 틀린 단지를 잡았다(10-24 지식iN, 심사도 통과).
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
                parts.append(f"{c['name']} {_plabel(py)} {eok(last['price'])}")
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
            areas = sorted(round(float(f.split("전용 ")[1].split("㎡")[0])) for f in facts)
            if band_all is None or not areas:
                head = "최근 실거래는"
            elif areas[0] == areas[-1]:
                head = f"같은 {areas[0]}㎡ 기준 최근 실거래는"
            else:   # 띠 안에서도 면적이 다르면 '같은'이라 쓰지 않는다(77㎡ vs 80㎡를 '같은 77㎡'로 썼다)
                head = f"전용 {areas[0]}~{areas[-1]}㎡ 기준 최근 실거래는"
        if len(parts) < 2:
            return {"ok": False, "why": "비교할 실거래가 부족합니다", "complexes": cs, "hubs": hs, "fit": 0, "data": 0}
        lines.append(f"{head} {', '.join(parts)}이에요.")
        link = shortlink.compare(cids) or tracked(compare_path(cids), channel, "compare", placement, qa)
        tail = f"{'두' if len(cids) == 2 else '세'} 단지 평형별 실거래 비교"   # 세 단지 비교에 '두 단지'라 썼다(10-25)
        fit, data = 3, 3
    elif len(cs) == 1:
        c = cs[0]
        ts = reevl.trades(c["cid"])
        pool = _in_pyeong(ts, pyeong[0]) if pyeong else []
        label = _plabel(pyeong[0]) if pool else ""
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


def _axis_facts(c: dict) -> list[str]:
    """단지 하나의 사실 — 세대수·연식·전세가율·1년 등락·학군/역세권 점수·월 관리비. 값이 없는 축은 뺀다(AI 예측·고점 대비는 쓰지 않는다)."""
    a = reevl.apartment(c["cid"])
    b, g, j, r, loc, bld = (a.get(k) or {} for k in ("기본", "가격", "전세월세", "등락", "입지", "건물"))
    n, out = c["name"], []
    if b.get("hh"):
        out.append(f"{n} 세대수 {b['hh']:,}세대")
    if b.get("age") is not None:
        out.append(f"{n} 준공 후 {b['age']}년")
    if j.get("jr"):
        out.append(f"{n} 전세가율 {j['jr']}%")
    if r.get("r12") is not None:
        out.append(f"{n} 최근 1년 매매가 등락 {r['r12']:+d}%")
    if loc.get("hakgun") is not None:
        out.append(f"{n} 리블 학군 점수 {loc['hakgun']}점(0~100)")
    if loc.get("elem") is not None or (a.get("주변") or {}).get("elem") is not None:
        out.append(f"{n} 리블 기준 초등학교까지 거리 {(a.get('주변') or {}).get('elem')}m")
    if loc.get("subway") is not None:
        out.append(f"{n} 리블 역세권 점수 {loc['subway']}점(0~100)")
    if bld.get("mgmt"):
        out.append(f"{n} 월 관리비 단지 평균 {round(bld['mgmt'] / 10000)}만원")
    return out


REGION_RE = re.compile(r"[가-힣]+(?:시|군|구)")


def build_link(text: str, entities: dict | None, channel: str = "naver_cafe", today: date | None = None) -> dict:
    """링크형 답변(10-30 사용자 제안) — 글이 말한 단지가 있으면 그 단지 페이지, 없으면 학교·역·지역 허브로 잇는다.
    비교를 묻더라도 단지가 하나만 확정되면 그 단지 페이지로 간다. 가격으로 답하지 못하는 질문(학군·세대수·연식·관리비 등)에는
    리블이 가진 사실(_axis_facts)을 한두 개 붙인다. 전망·추천·단정은 쓰지 않는다."""
    today = today or date.today()
    cs = []
    if entities and (entities.get("targets") or entities.get("complexes")):
        try:
            cs = match.complexes_from_entities(entities, text=text)
        except Exception:
            cs = []
    try:
        hs = hubs.find(text)
    except Exception:
        hs = []
    st = intent.style(channel)
    if cs:
        c = cs[0]
        facts = _axis_facts(c)
        ts = reevl.trades(c["cid"])
        if ts:
            last = max(ts, key=lambda t: t["date"])
            facts.append(_fact(c["name"], last, today))
        link = shortlink.compare([x["cid"] for x in cs[:3]]) if len(cs) >= 2 else _plain(complex_path(c["cid"]))
        return {"ok": True, "kind": "link", "complexes": cs[:3] if len(cs) >= 2 else [c], "hubs": hs, "links": [link],
                "facts": facts, "fit": 2, "data": 2, "match_via": "link",
                "text": f"{facts[0] if facts else c['name']}이에요.\n제가 운영하는 리블에 {c['name']} 실거래와 입지 정리해 뒀어요 {link}"}
    hub = next((h for h in hs if h["kind"] in ("midschool", "school", "highschool", "station", "region", "univ")), None)
    if not hub and entities and entities.get("sido") and entities.get("gu"):
        # 지역 허브 이름은 '서울 구로구' 형식이다. 추출한 시도·구가 허브 목록에 정확히 있을 때만 쓴다.
        want = f"{entities['sido'][:2]} {entities['gu']}"
        alias = {"서울": "서울", "경기": "경기", "인천": "인천", "부산": "부산"}
        for name in hubs.load().get("region", []):
            if name == want or (name.split(" ")[-1] == entities["gu"] and name.split(" ")[0] == alias.get(entities["sido"][:2], entities["sido"][:2])):
                hub = {"kind": "region", "name": name, "label": "지역", "path": "/region/" + urllib.parse.quote(name)}
                break
    if not hub:
        return {"ok": False, "why": "링크로 이을 단지·학교·역·지역을 찾지 못함", "complexes": [], "hubs": hs, "fit": 0, "data": 0}
    link = _plain(hub["path"])
    return {"ok": True, "kind": "hub", "complexes": [], "hubs": [hub], "links": [link], "fit": 1, "data": 1, "match_via": "hub",
            "facts": [f"{hub['name']} {hub['label']} 주변 단지 목록 페이지(리블)"],
            "text": f"{hub['name']} 주변 단지 실거래를 한 페이지에 모아 볼 수 있어요.\n제가 운영하는 리블에 {hub['name']} 주변 단지 정리해 뒀어요 {link}"}


def build_explore(text: str, entities: dict | None, post: dict, today: date | None = None) -> dict:
    """지역 조회형 — 글이 말한 시·구·동의 단지 목록에서 질문에 맞는 단지를 Claude가 고르고(judge.pick),
    사실(리블 대표가·대표 전용면적·세대수·준공 후 연수)은 코드가 데이터에서 직접 가져온다. 가격은 '리블 대표가'로 쓴다."""
    ents = entities or {}
    tg = [t for t in (ents.get("targets") or []) if isinstance(t, dict)]
    sido = ents.get("sido") or next((t.get("sido") for t in tg if t.get("sido")), None)
    gu = ents.get("gu") or next((t.get("gu") for t in tg if t.get("gu")), None)
    dong = ents.get("dong") or next((t.get("dong") for t in tg if t.get("dong")), None)
    if not (gu or dong):
        return {"ok": False, "why": "지역(시·구·동)을 확정하지 못해 조회형을 쓸 수 없음", "complexes": [], "hubs": [], "fit": 0, "data": 0}
    args = {"limit": 60, "sort": "hh", "fields": "cid,name,gu,dong,hh,age,price,area,ppm"}
    if sido: args["sido"] = sido
    if gu: args["gu"] = gu
    if dong: args["dong"] = dong
    try:
        items = reevl.call("search_apartments", **args).get("items", [])
    except Exception as e:
        return {"ok": False, "why": f"지역 조회 실패: {str(e)[:60]}", "complexes": [], "hubs": [], "fit": 0, "data": 0}
    items = [i for i in items if re.match(r"^\d{5}\|", i.get("cid", "")) and i.get("price")]
    if len(items) < 2:
        return {"ok": False, "why": "지역 조회 결과가 부족함", "complexes": [], "hubs": [], "fit": 0, "data": 0}
    cids, usd = judge.pick(post, items)
    by = {i["cid"]: i for i in items}
    picked = [by[c] for c in cids if c in by]
    if not picked:
        return {"ok": False, "why": "질문에 맞는 단지를 고르지 못함", "complexes": [], "hubs": [], "fit": 0, "data": 0, "spent": usd}
    facts = [f"{c['name']} 리블 대표가 {eok(c['price'])} 대표 전용 {c['area']}㎡ 기준 세대수 {c['hh']:,}세대 준공 후 {c['age']}년" for c in picked]
    cs = [{"cid": c["cid"], "name": c["name"], "gu": c.get("gu"), "dong": c.get("dong"), "hh": c.get("hh")} for c in picked]
    link = shortlink.compare([c["cid"] for c in picked]) if len(picked) >= 2 else _plain(complex_path(picked[0]["cid"]))
    n = len(picked)
    return {"ok": True, "kind": "explore", "complexes": cs, "hubs": [], "links": [link], "facts": facts, "fit": 2, "data": 2, "match_via": "explore", "spent": usd,
            "text": f"{facts[0]}이에요.\n제가 운영하는 리블에 {'두' if n == 2 else '세' if n == 3 else ''} 단지 비교를 정리해 뒀어요 {link}".replace("  ", " ")}
