"""공유 링크 — 리블의 기존 비교 딥링크 규격을 그대로 쓰고, 바깥으로 나가는 링크에만 UTM을 붙인다.

규격(리블 lib/routes.ts compareDeeplink): /share/compare?compare=<encode(cid)>!<encode(cid)>
/share/compare의 canonical은 홈이라 UTM이 붙은 주소가 색인 중복을 만들지 않는다(2026-10-24 확인).
"""
from __future__ import annotations

import urllib.parse

from . import CAMPAIGN, QA_CAMPAIGN

SITE = "https://reevl.co.kr"
SOURCES = {"naver_cafe", "naver_kin", "naver_blog", "daum_cafe", "daum_web", "ppomppu", "copy", "web_share"}
MEDIUM = {"naver_cafe": "community", "naver_kin": "community", "naver_blog": "community",
          "daum_cafe": "community", "daum_web": "community", "ppomppu": "community",
          "copy": "share", "web_share": "share"}
CONTENT = {"compare", "screener", "housing", "complex", "hub"}


def compare_path(cids: list[str]) -> str:
    if not 2 <= len(cids) <= 3:
        raise ValueError("비교는 2~3개 단지")
    return "/share/compare?compare=" + "!".join(urllib.parse.quote(c, safe="") for c in cids)


def complex_path(cid: str) -> str:
    return "/complex/" + urllib.parse.quote(cid, safe="")


def tracked(path: str, source: str, content: str, placement: str | None = None, qa: bool = False) -> str:
    """바깥에 게시할 링크. placement는 게시물 원문이 아닌 운영용 코드(예: cafe-0425-1)."""
    if source not in SOURCES:
        raise ValueError(f"source는 {sorted(SOURCES)} 중 하나")
    if content not in CONTENT:
        raise ValueError(f"content는 {sorted(CONTENT)} 중 하나")
    q = {"utm_source": source, "utm_medium": MEDIUM[source],
         "utm_campaign": QA_CAMPAIGN if qa else CAMPAIGN, "utm_content": content}
    if placement:
        q["utm_term"] = placement
    sep = "&" if "?" in path else "?"
    return SITE + path + sep + urllib.parse.urlencode(q)
