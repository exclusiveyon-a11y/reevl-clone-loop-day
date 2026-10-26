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


def _si_gu(item: dict) -> set[str]:
    """'고양시 덕양구' → {'고양시', '고양', '덕양구', '덕양'} — 이름 속 지역 토큰을 떼거나 원글에서 찾을 때 쓴다."""
    out = set()
    for w in (item.get("gu") or "").split():
        out |= _regions(w)
    return out


def _key(name: str, region: set[str]) -> str:
    """표기 차이를 지운 비교 키(10-25 부동산스터디 매칭 실패 48건에서 뽑은 유형).
    '무지개(3단지)(신한)'·'무지개마을3단지' → '무지개3' · '래미안영등포프레비뉴'·'신길래미안프레비뉴' → '래미안프레비뉴'
    '고양삼송동원로얄듀크'·'삼송마을동원로얄듀크' → '동원로얄듀크'. 지역 토큰은 앞뿐 아니라 가운데서도 뗀다."""
    n = re.sub(r"\s+", "", name or "")
    n = re.sub(r"\((\d+)(?:단지|차)\)", r"\1", n)         # 괄호 속 단지 번호는 살린다
    n = re.sub(r"\([^)]*\)", "", n)                       # 괄호 속 시공사('신한')는 글에 잘 안 나온다
    n = re.sub(r"(아파트|APT)$", "", n).replace("마을", "")
    n = re.sub(r"(\d+)(차|단지)", r"\1", n)
    for r in sorted(region, key=len, reverse=True):
        if len(r) >= 2 and r in n and len(n) - len(r) >= 3:
            n = n.replace(r, "", 1)
    return n


# 브랜드만으로는 같은 단지라 할 수 없다 — '이문아이파크'가 '아이파크'만 남아 '이문아이파크자이'에 걸렸다(10-26 평가)
BRANDS = set("아이파크 푸르지오 래미안 힐스테이트 롯데캐슬 두산위브 센트레빌 호반베르디움 꿈에그린 e편한세상 이편한세상 "
             "센트럴 더퍼스트 리버파크 하이츠 파크뷰 스카이뷰 그랑블 더클래스".split())


def _same(a: str, b: str) -> bool:
    """키가 같거나, 숫자(단지·차수)가 같고 한쪽이 다른 쪽을 4자 이상으로 품을 때('더샵분당하이스트' ⊃ '하이스트')."""
    if not a or not b:
        return False
    if a == b:
        return True
    if sorted(re.findall(r"\d+", a)) != sorted(re.findall(r"\d+", b)):
        return False                                    # '무지개3' ≠ '무지개12' · '동원로얄듀크' ≠ '동탄역동원로얄듀크1'
    sa, sb = sorted((re.sub(r"\d+", "", a), re.sub(r"\d+", "", b)), key=len)
    return len(sa) >= 4 and sa not in BRANDS and sa in sb


def _target(t: dict, ent: dict, text: str, budget: list[int]) -> dict | None:
    """단지 하나(글 표기 + 별칭 + 그 단지의 지역) → 리블 단지 하나. 확신이 없으면 None."""
    sido = t.get("sido") or ent.get("sido") or None
    gu = t.get("gu") or ent.get("gu") or None
    dong = t.get("dong") or ent.get("dong") or None
    names = []
    for n in [t.get("name")] + list(t.get("aliases") or []):
        if isinstance(n, str) and len(n.strip()) >= 2 and n.strip() not in names:
            names.append(n.strip())
    base = _regions(dong, gu) | {w for g in (gu or "").split() for w in _regions(g)}
    tried = set()
    for name in names:
        k = _key(name, base)
        bare = re.sub(r"\d+", "", k)
        qs = [re.sub(r"\s+", "", name), k, bare, bare[-4:] if len(bare) >= 6 else "", bare[:4] if len(bare) >= 6 else ""]
        for q in qs:
            if len(q) < 2 or q in tried or q in STOP or budget[0] <= 0:
                continue
            tried.add(q)
            budget[0] -= 1
            try:
                items = reevl.search(q, sido=sido, gu=gu, limit=30) if gu else reevl.search(q, sido=sido, limit=30)
            except Exception:
                items = []
            items = [i for i in items if _gu_ok(i.get("gu", ""), gu)] if gu else items
            # ★'u|…'는 입주 전 분양 단지다 — 실거래가 없어 get_transactions가 거부한다(10-26 '더샵 분당하이스트'·'라첼스')
            items = [i for i in items if re.match(r"^\d{5}\|", i.get("cid", ""))]
            hit = [i for i in items if _same(_key(i["name"], base | _regions(i.get("dong")) | _si_gu(i)), k)]
            if len(re.sub(r"\d+", "", k)) < 4:
                # 지역을 떼고 남은 이름이 짧으면('압구정현대' → '현대') 흔한 이름이다 — 동이 같거나, 뗀 동네가 리블 이름에도 있을 때만
                #   (10-26 평가: 압구정현대가 청담동 '현대아파트'로 잡혔다. '목동우성2차' ↔ 신정동 '목동우성2'는 이름의 '목동'으로 통과)
                #   뗀 것이 구 이름뿐이면('부평현대2차' → 부평구 안의 '현대2차', 산곡동) 구 조건이 이미 걸려 있으니 그대로 둔다.
                gu_tok = {w for g in (gu or "").split() for w in _regions(g)}
                cut = [r for r in base if len(r) >= 2 and r in re.sub(r"\s+", "", name) and r not in gu_tok]
                if cut or dong:
                    hit = [i for i in hit if (dong and _dong_ok(i.get("dong", ""), dong)) or any(r in i["name"] for r in cut)
                           or any(r in _regions(i.get("dong")) for r in cut)]
            if not hit:
                continue
            if len(hit) > 1:
                eq = [i for i in hit if _key(i["name"], base | _regions(i.get("dong")) | _si_gu(i)) == k]
                hit = eq or hit
            if len(hit) > 1 and dong:
                hit = [i for i in hit if _dong_ok(i.get("dong", ""), dong)] or hit
            if not gu:
                # 지역을 모르면 그 단지의 시·구 이름이 원글에 있을 때만 믿는다(전국 동명 단지 차단)
                hit = [i for i in hit if any(len(r) >= 2 and r in text for r in _si_gu(i))] if len(hit) > 1 or len(k) < 5 else hit
            if len(hit) == 1:
                return hit[0]
            return None                                  # 여럿이 남으면 고르지 않는다 — 다음 별칭으로 넘어가지도 않는다
    # ★Claude가 지역을 틀리게 줄 때가 있다('광교자이더클래스'를 수원시 영통구로 — 실제는 용인시 수지구 상현동, 10-26 평가).
    #   구 조건으로 못 찾았으면 긴 이름(6자 이상)이 전국에서 정확히 한 곳일 때만 받는다.
    if gu and budget[0] > 0:
        for name in names:
            k = _key(name, base)
            if len(re.sub(r"\d+", "", k)) < 6 or budget[0] <= 0:
                continue
            budget[0] -= 1
            try:
                items = reevl.search(re.sub(r"\s+", "", name), limit=30)
            except Exception:
                items = []
            eq = [i for i in items if re.match(r"^\d{5}\|", i.get("cid", ""))
                  and _key(i["name"], base | _regions(i.get("dong")) | _si_gu(i)) == k]
            if len(eq) == 1:
                return eq[0]
    return None


def complexes_from_targets(ent: dict, text: str = "", max_calls: int = 12) -> list[dict]:
    """Claude가 단지별로 나눠 준 targets(이름·별칭·지역)로 찾는다. 단지마다 지역이 달라도 된다
    (10-25 '분당하이스트 vs 테헤란아이파크'는 지역을 하나만 받아 강남구 단지를 버렸다)."""
    budget, found = [max_calls], []
    for t in (ent.get("targets") or [])[:3]:
        if not isinstance(t, dict):
            continue
        got = _target(t, ent, text, budget)
        if got and got["cid"] not in [f["cid"] for f in found]:
            found.append(got)
    return found


def complexes_from_entities(ent: dict, max_calls: int = 6, text: str = "") -> list[dict]:
    """Claude가 추출한 단지명·지역으로 찾는다. 결과의 구·동이 추출 지역과 다르면 버린다.
    targets(단지별 이름·별칭·지역)가 있으면 그쪽을 쓴다 — 별칭이 한 목록에 섞여 단지 수가 부풀지 않는다.

    ★오매칭 차단: '목동 진도2차'가 금천구 독산동 진도2차로 잡히던 것을 막는다(gu·dong 대조).
    ★표기 차이 흡수(2026-10-24 실측): 글은 '목동진도2차', 리블 이름은 '진도2'·'목동우성2' — 핵심 이름(_core)으로 맞춘다.
    지역이 하나도 없으면 이름이 정확히 같은 단지가 한 곳일 때만 고른다(애매하면 고르지 않는다)."""
    if ent.get("targets"):
        return complexes_from_targets(ent, text=text)
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
        # ★양쪽에서 같은 동네 접두를 뗀다 — 리블 '부개주공3'(부개동)은 '주공3'으로, 글의 '부개주공3'은 그대로라 못 맞췄다(10-25)
        ex = lambda xs: [i for i in xs if _core(i["name"], base | _regions(i.get("dong")))
                         in (core, _core(name, base | _regions(i.get("dong"))))]
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
