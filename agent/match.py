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
