"""허브 색인 — 리블 공개 사이트맵에서 허브 이름을 읽어 글 속 지명·학교·역·시공사를 리블 페이지로 잇는다.

캐시는 data/hubs.json(.gitignore 대상)에 하루 보관한다. 사이트맵은 공개 자료다.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request

SITEMAP = "https://reevl.co.kr/sitemap/hubs.xml"
CACHE = os.environ.get("GTM_HUB_CACHE", "data/hubs.json")
UA = "reevl-gtm-agent/0.1"
LABEL = {"school": "초등학교", "midschool": "중학교", "highschool": "고등학교", "univ": "대학교",
         "station": "역", "builder": "시공사", "brand": "브랜드", "region": "지역", "subway": "노선",
         "ic": "고속도로 IC", "airport": "공항", "highway": "고속도로"}
SIDO = ("서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "경기", "강원", "충북", "충남",
        "전북", "전남", "경북", "경남", "제주", "전남광주")


def load() -> dict[str, list[str]]:
    if os.path.exists(CACHE) and time.time() - os.path.getmtime(CACHE) < 86400:
        return json.load(open(CACHE, encoding="utf-8"))
    xml = urllib.request.urlopen(urllib.request.Request(SITEMAP, headers={"user-agent": UA}), timeout=30).read().decode()
    out: dict[str, list[str]] = {}
    for loc in re.findall(r"<loc>https://reevl\.co\.kr/([^<]+)</loc>", xml):
        kind, _, name = urllib.parse.unquote(loc).partition("/")
        if name:
            out.setdefault(kind, []).append(name)
    os.makedirs(os.path.dirname(CACHE) or ".", exist_ok=True)
    json.dump(out, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    return out


def _bare(name: str) -> str:
    """'서울구로남초' → '구로남초'처럼 앞의 시도 접두를 뗀 이름(글에서는 보통 접두 없이 쓴다)."""
    for s in SIDO:
        if name.startswith(s) and len(name) - len(s) >= 3:
            return name[len(s):]
    return name


def find(text: str) -> list[dict]:
    """글에 그대로 등장하는 허브만 고른다. 이름 유사도로 만들어 내지 않는다."""
    idx, hits = load(), []
    t = re.sub(r"\s+", "", text)
    for kind in ("midschool", "school", "highschool", "univ", "builder"):
        for name in idx.get(kind, []):
            key = _bare(name)
            if len(key) >= 3 and key in t:
                hits.append({"kind": kind, "name": name, "label": LABEL[kind], "path": f"/{kind}/{urllib.parse.quote(name)}"})
    stations = set(idx.get("station", []))
    for m in re.findall(r"([가-힣A-Za-z0-9]{2,10})역", text):
        if m in stations:
            hits.append({"kind": "station", "name": m, "label": "역", "path": f"/station/{urllib.parse.quote(m)}"})
    for name in idx.get("region", []):
        parts = name.split(" ")
        if len(parts) >= 2 and parts[0] in text and parts[-1] in text:
            hits.append({"kind": "region", "name": name, "label": "지역", "path": f"/region/{urllib.parse.quote(name)}"})
    seen, out = set(), []
    for h in hits:
        if h["path"] not in seen:
            seen.add(h["path"]); out.append(h)
    return out
