"""수요 찾기 — 공식 검색 API와 공개 RSS로 최근 부동산 질문 글을 찾는다. 로그인 수집·우회·비공식 스크래핑 없음.

provider(= channel 값):
  naver_cafe · naver_kin · naver_blog  네이버 검색 API(openapi.naver.com/v1/search/*.json)
                                        키 NAVER_CLIENT_ID / NAVER_CLIENT_SECRET, 무료 한도 하루 25,000회
  daum_cafe · daum_web                  다음 검색 API(dapi.kakao.com/v2/search/{cafe,web})
                                        키 KAKAO_REST_API_KEY, 헤더 'Authorization: KakaoAK <키>'
  ppomppu                               뽐뿌 부동산포럼 공개 RSS(rss.php?id=house) — 키 없음

★넣지 않은 것과 이유
  · 구글 Custom Search JSON API: 신규 가입이 막혔고(새 프로젝트는 403) 2027-01-01 종료 예정이다.
  · Brave 등: 카드 등록·종량 과금이 필요하다(무료로만 운영한다는 원칙).

키는 agent.config.get으로 실행 시점에만 읽고 출력하지 않는다. 키가 없는 provider는 경고 후 건너뛴다.
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from . import config

UA = "reevl-gtm-agent/0.1 (+https://github.com/exclusiveyon-a11y/reevl-clone-loop-day)"
KST = timezone(timedelta(hours=9))

NAVER_API = {"naver_cafe": "https://openapi.naver.com/v1/search/cafearticle.json",
             "naver_kin": "https://openapi.naver.com/v1/search/kin.json",
             "naver_blog": "https://openapi.naver.com/v1/search/blog.json"}
DAUM_API = {"daum_cafe": "https://dapi.kakao.com/v2/search/cafe",
            "daum_web": "https://dapi.kakao.com/v2/search/web"}
PPOMPPU_RSS = "https://www.ppomppu.co.kr/rss.php?id=house"

# 하위 호환 — 예전 이름
API = NAVER_API


def _clean(s: str | None) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def _warn(msg: str) -> None:
    print(f"[discover] ⚠ {msg}", file=sys.stderr)


def _get_json(url: str, headers: dict) -> dict:
    req = urllib.request.Request(url, headers={"user-agent": UA, **headers})
    return json.load(urllib.request.urlopen(req, timeout=20))


def _item(provider: str, *, url: str, title: str, desc: str, where: str, where_url: str,
          rank: int, query: str, posted_at: str = "") -> dict:
    return {"channel": provider, "url": url, "title": title, "desc": desc, "where": where,
            "where_url": where_url, "rank": rank, "query": query, "provider": provider,
            "posted_at": posted_at}


# ── 네이버 ────────────────────────────────────────────────────────────────

# NAVER API HUB(네이버 클라우드 중개) — 키 모양이 다르다(ID 10자·시크릿 40자). 경로에 .json이 없다.
HUB_HOST = "https://naverapihub.apigw.ntruss.com/search/v1/"
HUB_PATH = {"naver_cafe": "cafearticle", "naver_kin": "kin", "naver_blog": "blog"}
# ★과금 방지: 네이버 클라우드는 유료 플랫폼이다. 콘솔 무료 표시가 하루 25,000회라도 우리는 하루 300회로 묶는다.
HUB_DAILY_CAP = int(os.environ.get("GTM_NAVER_DAILY_CAP", "300"))
_CALLS = os.environ.get("GTM_NAVER_CALLS", "data/naver_calls.json")


def _hub_budget() -> None:
    """오늘(KST) 호출 수를 세고 상한이면 막는다. 실패해도 세지 않는 쪽보다 막는 쪽으로."""
    today = datetime.now(KST).strftime("%Y-%m-%d")
    try:
        d = json.load(open(_CALLS, encoding="utf-8"))
    except Exception:
        d = {}
    n = d.get(today, 0)
    if n >= HUB_DAILY_CAP:
        raise RuntimeError(f"네이버 검색 하루 상한 {HUB_DAILY_CAP}회 도달 — 내일 다시")
    os.makedirs(os.path.dirname(_CALLS) or ".", exist_ok=True)
    json.dump({today: n + 1}, open(_CALLS, "w", encoding="utf-8"))


def _naver_keys() -> tuple[str | None, str | None, bool]:
    """검색 전용 키(NAVER_SEARCH_* — NAVER API HUB)를 먼저 쓴다. 리블의 NAVER_CLIENT_*는 데이터랩용 개발자센터
    키라 검색 권한이 없다(2026-10-24 실측 401). 짝으로만 쓴다. 세 번째 값 = API HUB 여부."""
    sid, ssec = config.get("NAVER_SEARCH_CLIENT_ID"), config.get("NAVER_SEARCH_CLIENT_SECRET")
    if sid and ssec:
        return sid, ssec, True
    return config.get("NAVER_CLIENT_ID"), config.get("NAVER_CLIENT_SECRET"), False


def _naver(provider: str, query: str, n: int) -> list[dict]:
    cid, sec, hub = _naver_keys()
    if not cid or not sec:
        raise RuntimeError("NAVER_SEARCH_CLIENT_ID/SECRET 없음 — 리블 .env 또는 환경 변수에 둘 것")
    params = urllib.parse.urlencode({"query": query, "display": max(1, min(n, 100)), "sort": "date"})
    if hub:
        _hub_budget()
        items = _get_json(HUB_HOST + HUB_PATH[provider] + "?" + params,
                          {"X-NCP-APIGW-API-KEY-ID": cid, "X-NCP-APIGW-API-KEY": sec}).get("items", [])
    else:
        items = _get_json(NAVER_API[provider] + "?" + params,
                          {"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": sec}).get("items", [])
    out = []
    for rank, it in enumerate(items):
        if provider == "naver_cafe":
            where, where_url, posted = it.get("cafename", ""), it.get("cafeurl", ""), ""
        elif provider == "naver_blog":
            where, where_url = it.get("bloggername", ""), it.get("bloggerlink", "")
            pd = it.get("postdate", "")
            posted = f"{pd[:4]}-{pd[4:6]}-{pd[6:8]}" if re.fullmatch(r"\d{8}", pd or "") else ""
        else:  # naver_kin — 검색 API가 작성일을 주지 않는다
            where, where_url, posted = "지식iN", "https://kin.naver.com/", ""
        out.append(_item(provider, url=it.get("link", ""), title=_clean(it.get("title")),
                         desc=_clean(it.get("description")), where=where, where_url=where_url,
                         rank=rank, query=query, posted_at=posted))
    return out


# ── 다음(카카오) ──────────────────────────────────────────────────────────

def _daum(provider: str, query: str, n: int) -> list[dict]:
    key = config.get("KAKAO_REST_API_KEY")
    if not key:
        raise RuntimeError("KAKAO_REST_API_KEY 없음 — 리블 .env 또는 환경 변수에 둘 것")
    url = DAUM_API[provider] + "?" + urllib.parse.urlencode(
        {"query": query, "sort": "recency", "size": max(1, min(n, 50))})
    docs = _get_json(url, {"Authorization": f"KakaoAK {key}"}).get("documents", [])
    out = []
    for rank, d in enumerate(docs):
        link = d.get("url", "")
        if provider == "daum_cafe":
            where = d.get("cafename", "")
            where_url = ""
        else:
            host = urllib.parse.urlparse(link).netloc
            where, where_url = host, f"https://{host}/" if host else ""
        out.append(_item(provider, url=link, title=_clean(d.get("title")), desc=_clean(d.get("contents")),
                         where=where, where_url=where_url, rank=rank, query=query,
                         posted_at=d.get("datetime", "") or ""))
    return out


# ── 뽐뿌 부동산포럼 RSS ───────────────────────────────────────────────────

def _ppomppu_feed() -> list[dict]:
    req = urllib.request.Request(PPOMPPU_RSS, headers={"user-agent": UA})
    root = ET.fromstring(urllib.request.urlopen(req, timeout=20).read())
    out = []
    for it in root.iter("item"):
        pub = it.findtext("pubDate") or ""
        try:
            posted = parsedate_to_datetime(pub).astimezone(KST).isoformat(timespec="seconds") if pub else ""
        except (TypeError, ValueError):
            posted = ""
        out.append({"url": (it.findtext("link") or "").strip(), "title": _clean(it.findtext("title")),
                    "desc": _clean(it.findtext("description")), "posted_at": posted})
    return out


def _ppomppu_match(feed: list[dict], query: str) -> list[dict]:
    """질의의 모든 단어가 제목·본문에 있는 글만. 질의가 비면 전부."""
    words = [w.lower() for w in query.split() if w]
    out = []
    for e in feed:
        hay = (e["title"] + " " + e["desc"]).lower()
        if all(w in hay for w in words):
            out.append(_item("ppomppu", url=e["url"], title=e["title"], desc=e["desc"], where="뽐뿌 부동산포럼",
                             where_url="https://www.ppomppu.co.kr/zboard/zboard.php?id=house",
                             rank=len(out), query=query, posted_at=e["posted_at"]))
    return out


# ── 공개 인터페이스 ──────────────────────────────────────────────────────

ALL_PROVIDERS = ["naver_cafe", "naver_kin", "naver_blog", "daum_cafe", "daum_web", "ppomppu"]


def available() -> list[str]:
    """키가 있는 provider 전부 + ppomppu(공개 RSS)."""
    out = []
    cid, sec = _naver_keys()
    if cid and sec:
        out += ["naver_cafe", "naver_kin", "naver_blog"]
    if config.get("KAKAO_REST_API_KEY"):
        out += ["daum_cafe", "daum_web"]
    return out + ["ppomppu"]


def search(query: str, channel: str = "naver_cafe", n: int = 20) -> list[dict]:
    """한 provider·한 질의. 실패하면 예외를 그대로 올린다(하위 호환)."""
    if channel in NAVER_API:
        return _naver(channel, query, n)
    if channel in DAUM_API:
        return _daum(channel, query, n)
    if channel == "ppomppu":
        return _ppomppu_match(_ppomppu_feed(), query)[:n]
    raise ValueError(f"알 수 없는 provider: {channel} (가능: {ALL_PROVIDERS})")


def search_all(queries: list[str], providers: list[str] | None = None, n: int = 20) -> list[dict]:
    """여러 질의 × 여러 provider. url 기준 중복 제거. 한 provider가 실패해도 경고만 내고 계속한다."""
    if providers is None:
        providers = available()
        missing = [p for p in ALL_PROVIDERS if p not in providers]
        if missing:
            _warn(f"키가 없어 건너뜀: {', '.join(missing)}")
    seen: set[str] = set()
    out: list[dict] = []
    feed: list[dict] | None = None
    dead: set[str] = set()
    for p in providers:
        if p not in ALL_PROVIDERS:
            _warn(f"알 수 없는 provider 건너뜀: {p}")
            continue
        for q in queries:
            if p in dead:
                break
            try:
                if p == "ppomppu":
                    if feed is None:
                        feed = _ppomppu_feed()
                    items = _ppomppu_match(feed, q)[:n]
                else:
                    items = search(q, p, n)
            except Exception as e:  # noqa: BLE001 — 한 provider 실패가 전체를 죽이지 않는다
                _warn(f"{p} 실패({type(e).__name__}: {str(e)[:120]}) — 이 provider는 건너뜀")
                dead.add(p)
                continue
            for it in items:
                key = it["url"].strip().rstrip("/")
                if key and key not in seen:
                    seen.add(key)
                    out.append(it)
    return out
