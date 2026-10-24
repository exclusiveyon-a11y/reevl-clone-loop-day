"""GA4 읽기 전용 리포트 — 해커톤 지표를 같은 필터·같은 정의로 뽑는다.

자격 증명은 저장소에 두지 않는다. 환경 변수로만 받는다.
  GA4_PROPERTY_ID                 GA4 속성 번호
  GOOGLE_APPLICATION_CREDENTIALS  analytics.readonly 권한이 있는 서비스 계정 JSON 경로

실행:
  pip install google-auth
  python analysis/ga_report.py                  # 기본: 최근 7일
  python analysis/ga_report.py --start 2026-10-27 --end 2026-11-02

출력은 화면에만 찍는다. 리퍼러의 쿼리 문자열(카페 토큰 등)은 잘라서 보여준다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

# 사람 필터 — 리블 운영 대시보드와 같은 정의
BOT_CITIES = ["Council Bluffs"]           # 데이터센터 봇
BOT_COUNTRIES = ["China"]                 # 헤드리스 봇
BOT_SCREENS = ["1600x1600"]               # 사람 모니터에 없는 해상도


def _not_in(field: str, values: list[str]) -> dict:
    return {"notExpression": {"filter": {"fieldName": field, "inListFilter": {"values": values}}}}


HUMAN = [_not_in("city", BOT_CITIES), _not_in("country", BOT_COUNTRIES), _not_in("screenResolution", BOT_SCREENS)]


def _token() -> str:
    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not path:
        sys.exit("GOOGLE_APPLICATION_CREDENTIALS가 없습니다")
    from google.oauth2 import service_account
    import google.auth.transport.requests as gart
    cred = service_account.Credentials.from_service_account_file(
        path, scopes=["https://www.googleapis.com/auth/analytics.readonly"])
    cred.refresh(gart.Request())
    return cred.token


def _run(token: str, pid: str, body: dict) -> list[dict]:
    req = urllib.request.Request(
        f"https://analyticsdata.googleapis.com/v1beta/properties/{pid}:runReport",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=60)).get("rows", [])


def _clean(v: str) -> str:
    return v.split("?", 1)[0] if v.startswith("http") else v


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="7daysAgo")
    ap.add_argument("--end", default="yesterday")
    a = ap.parse_args()
    pid = os.environ.get("GA4_PROPERTY_ID") or sys.exit("GA4_PROPERTY_ID가 없습니다")
    tok = _token()
    rng = [{"startDate": a.start, "endDate": a.end}]

    ev = lambda name: {"filter": {"fieldName": "eventName", "stringFilter": {"value": name}}}
    lp = lambda p: {"filter": {"fieldName": "landingPage", "stringFilter": {"matchType": "BEGINS_WITH", "value": p}}}
    src = lambda s: {"filter": {"fieldName": "sessionSource", "stringFilter": {"value": s}}}
    camp = {"filter": {"fieldName": "sessionCampaignName", "stringFilter": {"value": "gtm_7d_20261024"}}}
    qa = {"filter": {"fieldName": "sessionCampaignName", "stringFilter": {"value": "gtm_qa"}}}
    quality = ["sessions", "engagementRate", "averageSessionDuration", "screenPageViewsPerSession", "bounceRate"]

    reports = [
        ("전체 질", [], quality, []),
        ("일별", ["date"], ["activeUsers", "sessions", "screenPageViews", "averageSessionDuration", "engagementRate"], []),
        ("캠페인 gtm_7d_20261024 출처·위치별", ["sessionSource", "sessionMedium", "sessionManualAdContent", "sessionManualTerm"], quality, [camp]),
        ("테스트 gtm_qa (실험 집계에서 제외)", ["sessionSource", "sessionManualAdContent"], quality, [qa]),
        ("공유 이벤트", ["eventName"], ["eventCount", "totalUsers"], [ev("share")]),
        ("재방문 이벤트", ["eventName"], ["eventCount", "totalUsers"], [ev("return_visit")]),
        ("관심 저장", ["eventName"], ["eventCount", "totalUsers"], [ev("save_item")]),
        ("공유 UTM 세션", ["sessionSource"], quality, [src("reevl_share")]),
        ("카카오 공유 세션", ["sessionSource"], quality, [src("kakao")]),
        ("착지 /share/compare", [], quality, [lp("/share/compare")]),
        ("착지 /housing/", [], quality, [lp("/housing/")]),
        ("착지 /complex/", [], quality, [lp("/complex/")]),
        ("외부 리퍼러 (세션 시작)", ["pageReferrer"], ["eventCount"], [ev("session_start")]),
    ]
    for title, dims, mets, extra in reports:
        body = {"dateRanges": rng, "dimensions": [{"name": d} for d in dims],
                "metrics": [{"name": m} for m in mets], "limit": 40,
                "dimensionFilter": {"andGroup": {"expressions": HUMAN + extra}}}
        if dims == ["date"]:
            body["orderBys"] = [{"dimension": {"dimensionName": "date"}}]
        elif mets:
            body["orderBys"] = [{"metric": {"metricName": mets[0]}, "desc": True}]
        print(f"\n## {title}  ({a.start} ~ {a.end})")
        try:
            rows = _run(tok, pid, body)
        except Exception as e:  # 맞춤 측정기준 미등록 등
            print(f"  오류: {str(e)[:160]}")
            continue
        if not rows:
            print("  (없음)")
        for r in rows:
            k = " | ".join(_clean(v["value"]) for v in r.get("dimensionValues", []))
            v = " · ".join(v["value"] for v in r["metricValues"])
            print(f"  {k[:120]}  →  {v}")


if __name__ == "__main__":
    main()
