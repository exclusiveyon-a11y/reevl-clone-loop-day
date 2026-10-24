"""짧은 링크 — 리블에 이미 있는 비교 단축 링크(/c/<키>)를 쓴다. 새 단축 서비스를 만들지 않는다.

- 발급: POST https://reevl.co.kr/api/share/compare {cids} → {ok, key}. 리블 화면의 '비교 공유' 버튼과 같은 기능이다.
- 같은 조합은 같은 키를 돌려준다(리블 쪽 유일 인덱스) → 여러 번 불러도 행이 늘지 않는다.
- 로컬 캐시(data/short_cache.json)로 같은 조합은 다시 묻지 않는다.
- ★/c/<키>는 이동하면서 쿼리(UTM)를 버린다. 유입은 카페 리퍼러로 센다. UTM까지 살리려면 리블 쪽 라우트 수정이 필요하다.
- 발급이 실패하면 긴 비교 주소로 되돌아간다(링크를 잃지 않는다).
"""
from __future__ import annotations

import json
import os
import urllib.request

SITE = os.environ.get("REEVL_SITE", "https://reevl.co.kr")
CACHE = os.environ.get("GTM_SHORT_CACHE", "data/short_cache.json")
UA = "reevl-gtm-agent/0.1"


def _load() -> dict:
    try:
        return json.load(open(CACHE, encoding="utf-8"))
    except Exception:
        return {}


def compare(cids: list[str]) -> str | None:
    key = "!".join(cids)
    cache = _load()
    if key in cache:
        return cache[key]
    req = urllib.request.Request(SITE + "/api/share/compare", data=json.dumps({"cids": cids}).encode(),
                                 headers={"content-type": "application/json", "user-agent": UA}, method="POST")
    try:
        d = json.load(urllib.request.urlopen(req, timeout=20))
    except Exception:
        return None
    if not (d.get("ok") and d.get("key")):
        return None
    url = f"{SITE}/c/{d['key']}"
    cache[key] = url
    os.makedirs(os.path.dirname(CACHE) or ".", exist_ok=True)
    json.dump(cache, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    return url
