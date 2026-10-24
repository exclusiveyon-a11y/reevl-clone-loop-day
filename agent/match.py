"""글 → 리블 단지 찾기. 지역(구·동) 맥락이 있을 때만 이름 검색을 믿는다.

전국 단위로 '삼성'을 찾으면 수십 곳이 나온다. 그래서 글에 구나 동이 없으면 이름이 정확히 같은 단지가
한 곳일 때만 고른다. 애매하면 고르지 않는다(틀린 단지를 링크하는 것보다 링크가 없는 편이 낫다).
"""
from __future__ import annotations

import re

from . import reevl

STOP = set("아파트 단지 비교 고민 어디 어떤 추천 실거주 매매 전세 월세 투자 가격 시세 질문 문의 지금 요즘 근처 주변 "
           "학군 학교 배정 출퇴근 역세권 신축 구축 재건축 리모델링 분양 청약 정도 이랑 사이 중에 어느 쪽 좋을까요 "
           "좋은가요 괜찮을까요 있을까요 알려주세요 궁금합니다 생각 예산 대출 평수 평형".split())


def regions(text: str) -> tuple[str | None, str | None]:
    gu = re.search(r"([가-힣]{1,5}(?:구|시|군))(?=\s|$|[^가-힣])", text)
    dong = re.search(r"([가-힣0-9]{1,6}동)(?=\s|$|[^가-힣])", text)
    return (gu.group(1) if gu else None), (dong.group(1) if dong else None)


def tokens(text: str) -> list[str]:
    words = re.findall(r"[가-힣A-Za-z0-9]{2,14}", text)
    out = []
    for w in words:
        w = re.sub(r"(아파트|이랑|랑|하고|과|와|은|는|이|가|을|를|의|에서|에)$", "", w)
        if len(w) >= 2 and w not in STOP and not re.search(r"(구|시|군|동|역|초|중|고)$", w) and w not in out:
            out.append(w)
    return out[:8]


def complexes(text: str, max_calls: int = 8) -> list[dict]:
    gu, dong = regions(text)
    found, calls = [], 0
    for tok in tokens(text):
        if calls >= max_calls:
            break
        calls += 1
        try:
            items = reevl.search(tok, gu=gu, limit=10) if gu else reevl.search(tok, limit=10)
        except Exception:
            continue
        if dong:
            items = [i for i in items if i.get("dong") == dong] or items
        exact = [i for i in items if i["name"].replace("아파트", "") == tok]
        near = [i for i in items if tok in i["name"] and len(i["name"]) <= len(tok) + 6]
        pick = exact or (near if (gu or dong) else [])
        if len(pick) > 1 and not (gu or dong):
            continue                     # 지역 맥락 없이 여러 곳이면 고르지 않는다
        if pick:
            best = max(pick, key=lambda i: i.get("hh") or 0)
            if best["cid"] not in [f["cid"] for f in found]:
                found.append(best)
    return found[:3]


def _norm(s: str | None) -> str:
    return re.sub(r"\s+|아파트$", "", (s or "").strip())


def _gu_ok(item_gu: str, gu: str | None) -> bool:
    """'양천구' ↔ '양천구', '성남시 분당구' ↔ '분당구' 처럼 끝 토큰이 같으면 같은 구로 본다."""
    if not gu:
        return True
    a, b = _norm(item_gu), _norm(gu)
    return bool(a) and (a == b or a.endswith(b) or b.endswith(a.split("시")[-1]))


def _dong_ok(item_dong: str, dong: str | None) -> bool:
    """'목동' ↔ '목동'·'목1동'처럼 앞 이름이 같으면 같은 동으로 본다(행정동 번호 차이 허용)."""
    if not dong:
        return True
    a, b = _norm(item_dong), _norm(dong)
    base = lambda x: re.sub(r"\d*동$", "", x)
    return bool(a) and (a == b or base(a) == base(b))


def _core(name: str, region: set[str]) -> str:
    """비교용 핵심 이름 — 공백·'아파트'·끝의 '차'를 떼고, 앞에 붙은 지역 이름(목동·신정 등)을 뗀다.
    '목동진도2차' → '진도2', '목동우성2' → '우성2', '진도2' → '진도2'."""
    n = _norm(name)
    n = re.sub(r"(아파트|APT)$", "", n)
    n = re.sub(r"(\d+)차$", r"\1", n)
    for r in sorted(region, key=len, reverse=True):
        if r and n.startswith(r) and len(n) - len(r) >= 2:
            n = n[len(r):]
            break
    return n


def _regions(*names: str | None) -> set[str]:
    out = set()
    for x in names:
        x = (x or "").strip()
        if not x:
            continue
        out.add(x)
        out.add(re.sub(r"(동|구|시|군)$", "", x))
        out.add(re.sub(r"\d+(동)$", r"\1", x))      # '신정1동' → '신정동'
        out.add(re.sub(r"\d+동$", "", x))
    return {r for r in out if len(r) >= 2}


def complexes_from_entities(ent: dict, max_calls: int = 6) -> list[dict]:
    """Claude가 추출한 단지명·지역으로 찾는다. 결과의 구·동이 추출 지역과 다르면 버린다.

    ★오매칭 차단: '목동 진도2차'가 금천구 독산동 진도2차로 잡히던 것을 막는다(gu·dong 대조).
    ★표기 차이 흡수(2026-10-24 실측): 글은 '목동진도2차', 리블 이름은 '진도2'·'목동우성2' — 핵심 이름(_core)으로 맞춘다.
    지역이 하나도 없으면 이름이 정확히 같은 단지가 한 곳일 때만 고른다(애매하면 고르지 않는다)."""
    names = [n for n in (ent.get("complexes") or []) if isinstance(n, str) and n.strip()]
    gu, dong, sido = ent.get("gu") or None, ent.get("dong") or None, ent.get("sido") or None
    found, calls = [], 0
    for name in names:
        if calls >= max_calls:
            break
        base = _regions(dong, gu) | {"목동", "신정", "신월", "잠실", "개포", "반포", "대치", "상계", "중계", "하계"}
        core = _core(name, base)
        q = re.sub(r"\d+$", "", core) or core            # '진도2' → '진도'로 넓게 찾고 아래에서 핵심 이름으로 거른다
        calls += 1
        try:
            items = reevl.search(q, sido=sido, gu=gu, limit=20) if gu else reevl.search(q, sido=sido, limit=20)
        except Exception:
            items = []
        in_gu = [i for i in items if _gu_ok(i.get("gu", ""), gu)]
        in_dong = [i for i in in_gu if _dong_ok(i.get("dong", ""), dong)]
        ex = lambda xs: [i for i in xs if _core(i["name"], base | _regions(i.get("dong"))) == core]
        # ★동 조건으로 0건이면 같은 구 안에서 다시 — '목동' 신시가지는 법정동 목동·신정동에 걸쳐 있다(목동우성2=신정동)
        exact = ex(in_dong) or (ex(in_gu) if gu else [])
        pick = exact
        if len(pick) > 1:
            # ★이름 앞에 붙은 동네('목동진도2차'의 '목동')를 단서로 좁힌다 — 동 이름이 같거나 단지명에 그 동네가 들어간 곳
            pre = _norm(name)[: max(0, len(_norm(name)) - len(core) - (1 if _norm(name).endswith("차") else 0))]
            pre = re.sub(r"(동)$", "", pre)
            if len(pre) >= 2:
                hint = [i for i in pick if (i.get("dong") or "").startswith(pre) or pre in i["name"]]
                if hint:
                    pick = hint
        if len(pick) > 1 and not (gu or dong):
            continue
        if pick:
            best = max(pick, key=lambda i: i.get("hh") or 0)
            if best["cid"] not in [f["cid"] for f in found]:
                found.append(best)
    return found[:3]
