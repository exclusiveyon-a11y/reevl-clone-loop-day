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
# 지역 이름이 단지 이름과 같은 경우가 있다('김포'·'경기'라는 단지) — 10-24 지식iN에서 지역 질문을 그 단지로 잡았다.
STOP |= set("서울 부산 대구 인천 광주 대전 울산 세종 경기 강원 충북 충남 전북 전남 경북 경남 제주 수도권 지방 "
            "김포 파주 운정 일산 고양 분당 판교 성남 용인 수원 광교 동탄 화성 평택 안양 평촌 의왕 군포 산본 과천 "
            "하남 위례 광명 부천 시흥 안산 의정부 양주 남양주 구리 송도 청라 검단 원주 천안 청주 창원 포항 전주".split())


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
    if not a:
        return False
    if a == b:
        return True
    # '성남시분당구' ↔ '분당구'는 같게, '남양주시' ↔ '양주시'는 다르게 — 접미 앞이 '시'로 끝날 때만 같은 구(10-24 덕소→양주 오매칭)
    if a.endswith(b) and a[: -len(b)].endswith("시"):
        return True
    tail = a.split("시")[-1]
    return bool(tail) and b.endswith(tail) and b[: -len(tail)].endswith("시")


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
    n = re.sub(r"(\d+)(차|단지)$", r"\1", n)        # '덕소주공3단지' ↔ 리블 '덕소주공3' 
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


def complexes_from_entities(ent: dict, max_calls: int = 6, text: str = "") -> list[dict]:
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
        region_ok = False
        if not exact and text and len(core) >= 4:
            # ★다른 도시 단지끼리 비교('김포 고촌센트럴자이 vs 의정부 고산수자인', 10-24 지식iN) — 추출 지역(김포)로는 못 찾는다.
            #   전국에서 다시 찾되, 그 단지의 시·구 이름이 원글에 그대로 나올 때만 받는다. 이름은 핵심 이름이 앞부분과 같으면 같은 단지군으로 본다.
            try:
                wide = reevl.search(core, limit=20)
            except Exception:
                wide = []
            calls += 1
            fam = [i for i in wide if _core(i["name"], _regions(i.get("dong"))).startswith(core)
                   and any(len(r) >= 2 and r in text for r in _regions(i.get("gu")) | {(i.get("gu") or "").split(" ")[0]})]
            if len({i.get("gu") for i in fam}) == 1:
                exact, region_ok = fam, True      # 시·구는 원글로 확인됨 — 같은 단지군 안에서는 세대수 큰 곳
        pick = exact
        if len(pick) > 1:
            # ★이름 앞에 붙은 동네('목동진도2차'의 '목동')를 단서로 좁힌다 — 동 이름이 같거나 단지명에 그 동네가 들어간 곳
            pre = _norm(name)[: max(0, len(_norm(name)) - len(core) - (1 if _norm(name).endswith("차") else 0))]
            pre = re.sub(r"(동)$", "", pre)
            if len(pre) >= 2:
                hint = [i for i in pick if (i.get("dong") or "").startswith(pre) or pre in i["name"]]
                if hint:
                    pick = hint
        if len(pick) > 1 and not (gu or dong or region_ok):
            continue
        if pick:
            best = max(pick, key=lambda i: i.get("hh") or 0)
            if best["cid"] not in [f["cid"] for f in found]:
                found.append(best)
    return found[:3]
