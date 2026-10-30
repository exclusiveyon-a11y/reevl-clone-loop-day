"""댓글별 지속 유입(D0~D5) — 댓글 링크(/c/키)가 여는 비교 화면 주소로 들어온 세션을 게시일 기준 날짜별로 센다.

리블 /c/ 단축 링크는 이동하면서 UTM을 버리므로, 이동 주소의 compare=<cid>!<cid> 값을 GA4 랜딩(landingPagePlusQueryString)에서 찾는다.
같은 비교를 다른 곳에서 공유해도 섞인다(우리 댓글만의 값이 아니다) — 낮은 쪽이 아니라 높은 쪽으로 틀릴 수 있는 추정이다.
출력은 게시물 식별 없이 D0~D5 합계와 댓글별 합계(순번)만 낸다.
자격 증명은 환경 변수(GA4_PROPERTY_ID·GOOGLE_APPLICATION_CREDENTIALS)로만 받는다 — analysis/ga_report.py와 같다.
사용:  PYTHONPATH=. python analysis/ga_persistence.py [--end 2026-10-30]
"""
from __future__ import annotations

import argparse
import os
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

from agent.queue import load
from analysis.ga_report import HUMAN, _run, _token


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def compare_param(link: str) -> str:
    """/c/키 → 이동 주소의 compare= 뒤 값(인코딩된 그대로)."""
    try:
        urllib.request.build_opener(_NoRedirect).open(urllib.request.Request(link, headers={"User-Agent": "reevl-gtm-agent/0.1"}), timeout=10)
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location") or ""
        return loc.split("compare=", 1)[1].split("&", 1)[0] if "compare=" in loc else ""
    return ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--end", default=date.today().isoformat())
    a = ap.parse_args()
    pid, tok = os.environ["GA4_PROPERTY_ID"], _token()
    rows = [r for r in load() if r["status"] == "posted" and r["link"].startswith("https://reevl.co.kr/c/")]
    tot = {d: 0 for d in range(6)}
    per = []
    for i, r in enumerate(sorted(rows, key=lambda r: r["posted_at"]), 1):
        start = datetime.strptime(r["posted_at"][:10], "%Y-%m-%d").date()
        cp = compare_param(r["link"])
        if not cp:
            per.append((i, "링크 해석 실패")); continue
        flt = {"andGroup": {"expressions": HUMAN + [{"filter": {"fieldName": "landingPagePlusQueryString", "stringFilter": {"matchType": "CONTAINS", "value": cp}}}]}}
        body = {"dateRanges": [{"startDate": start.isoformat(), "endDate": a.end}], "dimensions": [{"name": "date"}],
                "metrics": [{"name": "sessions"}, {"name": "engagedSessions"}, {"name": "screenPageViews"}], "dimensionFilter": flt, "limit": 100}
        days = {}
        for row in _run(tok, pid, body):
            d = (datetime.strptime(row["dimensionValues"][0]["value"], "%Y%m%d").date() - start).days
            m = [int(x["value"]) for x in row["metricValues"]]
            days[d] = m
        for d, m in days.items():
            if 0 <= d <= 5:
                tot[d] += m[0]
        s = sum(m[0] for m in days.values())
        per.append((i, s, {d: days[d][0] for d in sorted(days)}))
    print("댓글별 합계(게시 순)")
    for p in per:
        print(" ", *p)
    allp = sum(tot.values())
    print("D0~D5 세션:", tot, "합계", allp, "· D2 이후 비중", f"{sum(v for d, v in tot.items() if d >= 2)}/{allp}" if allp else "-")


if __name__ == "__main__":
    main()
