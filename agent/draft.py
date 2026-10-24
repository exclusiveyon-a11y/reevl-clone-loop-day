"""답변 초안 — 같은 전용면적대의 확인된 실거래만으로 비교한다.

원칙(실행안 §3·§6):
- 관계 공개 → 질문 조건 요약 → 확인 가능한 수치 2~3개와 기준일 → 비교의 한계 → 링크 1개.
- 거래일·전용면적·층·가격·표본 수를 함께 적는다. AI 전망·순위·호가는 넣지 않는다.
- 표본이 없으면 없다고 쓴다. 빈 수치나 자리표시자를 내보내지 않는다.
- 매수 추천·상승 보장·학군 배정 확정 표현을 쓰지 않는다.
"""
from __future__ import annotations

from datetime import date, timedelta

from . import reevl
from .links import compare_path, tracked

BAND = 3.0          # 같은 면적대로 보는 전용면적 차이(㎡)
RECENT_DAYS = 365   # 표본 수를 세는 기간


def won(man: int) -> str:
    """만원 → '14억', '13억 5,000만원'."""
    eok, rest = divmod(int(man), 10000)
    if eok and rest:
        return f"{eok}억 {rest:,}만원"
    return f"{eok}억" if eok else f"{rest:,}만원"


def _recent(ts: list[dict], today: date) -> list[dict]:
    cut = (today - timedelta(days=RECENT_DAYS)).isoformat()
    return [t for t in ts if t.get("date", "") >= cut]


def pick_band(all_trades: list[list[dict]], today: date) -> float | None:
    """모든 단지에 거래가 있는 면적대 중 최근 1년 거래가 가장 많은 것. 동률이면 84㎡에 가까운 쪽."""
    best, best_key = None, None
    for t in all_trades[0]:
        a = t["area_m2"]
        if not all(any(abs(x["area_m2"] - a) <= BAND for x in ts) for ts in all_trades[1:]):
            continue
        n = sum(sum(abs(x["area_m2"] - a) <= BAND for x in _recent(ts, today)) for ts in all_trades)
        key = (n, -abs(a - 84))
        if best_key is None or key > best_key:
            best, best_key = a, key
    return best


def build(cids: list[str], source: str = "copy", placement: str | None = None,
          question: str | None = None, today: date | None = None, qa: bool = False) -> dict:
    today = today or date.today()
    apts = [reevl.apartment(c) for c in cids]
    tss = [reevl.trades(c) for c in cids]
    band = pick_band(tss, today) if all(tss) else None
    rows, missing = [], []
    for apt, ts in zip(apts, tss):
        b = apt.get("기본", {})
        label = f"{b.get('name', apt.get('name'))}({b.get('dong', '')}, {b.get('hh', '?')}세대·{b.get('age', '?')}년차)"
        same = [t for t in ts if band is not None and abs(t["area_m2"] - band) <= BAND]
        pool = same or ts
        if not pool:
            missing.append(label)
            rows.append(f"- {label}: 확인된 매매 실거래가 없습니다.")
            continue
        last = max(pool, key=lambda t: t["date"])
        n = len(_recent(pool, today))
        scope = "이 면적 거래" if same else "전체 면적 거래"   # 면적대가 안 겹치면 '이 면적'이라 쓰지 않는다
        rows.append(f"- {label}: 최근 {last['date']} {won(last['price'])} "
                    f"({last['floor']}층, 전용 {last['area_m2']}㎡) · 최근 1년 {scope} {n}건")
    link = tracked(compare_path(cids), source, "compare", placement, qa)
    names = " · ".join(a.get("기본", {}).get("name", a.get("name")) for a in apts)
    head = (f"같은 전용 {round(band)}㎡ 안팎으로 맞춰 비교해 보면 이렇습니다."
            if band is not None else "같은 면적대 거래가 겹치지 않아, 각 단지의 최근 거래를 그대로 적습니다.")
    limits = "거래 시점·층·표본 수가 달라 단순 우열로 볼 수 없습니다."
    if missing:
        limits += " 일부 단지는 확인된 거래가 없어 비교에서 빠졌습니다."
    text = "\n".join([
        "리블(reevl.co.kr)을 운영하고 있어 관계를 먼저 밝힙니다.",
        *( [f"질문 요약: {question}"] if question else [] ),
        f"{names} — {head}",
        *rows,
        f"{limits} 조회 기준일 {today.isoformat()}, 원자료 국토교통부 실거래가(해제 신고 제외).",
        f"조건과 비교표: {link}",
    ])
    assert "[" not in text and "None" not in text, "자리표시자·빈 값이 남았습니다"
    return {"text": text, "link": link, "band_m2": band, "cids": cids, "as_of": today.isoformat()}
