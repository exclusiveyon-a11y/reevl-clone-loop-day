"""수요 찾기 — 공식 검색 API와 공개 RSS로 최근 부동산 질문 글을 찾는다. 로그인 수집·우회·비공식 스크래핑 없음.

provider(= channel 값):
  naver_cafe · naver_kin · naver_blog  네이버 검색 API(openapi.naver.com/v1/search/*.json)
                                        키 NAVER_CLIENT_ID / NAVER_CLIENT_SECRET, 무료 한도 하루 25,000회
  daum_cafe · daum_web                  다음 검색 API(dapi.kakao.com/v2/search/{cafe,web})
                                        키 KAKAO_REST_API_KEY, 헤더 'Authorization: KakaoAK <키>'
  ppomppu                               뽐뿌 부동산포럼 공개 RSS(rss.php?id=house) — 키 없음
  community                             국내 커뮤니티 — 다음 웹검색에 `site:<도메인>`을 붙여 찾는다(KAKAO_REST_API_KEY).
                                        각 결과의 channel은 도메인별 이름(clien·ppomppu_web·dcinside·fmkorea·theqoo·
                                        daangn·blind·mlbpark·bobaedream·ruliweb). 다음 호출은 하루 500회 상한(data/daum_calls.json).
  youtube                               유튜브 — YouTube Data API v3(GOOGLE_API_KEY). 최근 영상의 댓글 중 질문 신호가
                                        있는 것만 후보로. 하루 3,000단위 상한(무료 10,000의 30%, data/youtube_units.json).

★넣지 않은 것과 이유
  · 구글 Custom Search JSON API: 신규 가입이 막혔고(새 프로젝트는 403) 2027-01-01 종료 예정이다.
  · Brave 등: 카드 등록·종량 과금이 필요하다(무료로만 운영한다는 원칙).
  · 82cook·instiz: 다음 site: 검색 결과 0건(2026-10-24 시험) — 넣지 않는다.
  · 당근: 공식 API가 없다. 다음 검색에 색인된 동네생활 글만 잡힌다(적다).

키는 agent.config.get으로 실행 시점에만 읽고 출력하지 않는다. 키가 없는 provider는 경고 후 건너뛴다.
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from . import config

UA = "reevl-gtm-agent/0.1 (+https://github.com/exclusiveyon-a11y/reevl-gtm-loop)"
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
          rank: int, query: str, posted_at: str = "", channel: str | None = None) -> dict:
    return {"channel": channel or provider, "url": url, "title": title, "desc": desc, "where": where,
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


def _budget(path: str, cap: int, cost: int, label: str) -> None:
    """오늘(KST) 사용량을 세고 상한을 넘으면 막는다. 세지 못하는 쪽보다 막는 쪽으로."""
    today = datetime.now(KST).strftime("%Y-%m-%d")
    try:
        d = json.load(open(path, encoding="utf-8"))
    except Exception:
        d = {}
    n = d.get(today, 0)
    if n + cost > cap:
        raise RuntimeError(f"{label} 하루 상한 {cap} 도달(오늘 {n}) — 내일 다시")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    json.dump({today: n + cost}, open(path, "w", encoding="utf-8"))


DAUM_DAILY_CAP = int(os.environ.get("GTM_DAUM_DAILY_CAP", "500"))
_DAUM_CALLS = os.environ.get("GTM_DAUM_CALLS", "data/daum_calls.json")


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
        link = it.get("link", "")
        if provider == "naver_kin":   # 같은 질문이 답변마다(&answerNo=N) 따로 나온다 — 질문 주소로 합친다
            link = re.sub(r"&answerNo=\d+", "", link)
        out.append(_item(provider, url=link, title=_clean(it.get("title")),
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
    _budget(_DAUM_CALLS, DAUM_DAILY_CAP, 1, "다음 검색")
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


# ── 국내 커뮤니티(다음 웹검색 site:) ───────────────────────────────────────
# (도메인, channel, 표시명) — 2026-10-24 다음 site: 검색으로 결과가 오는 것만(82cook·instiz는 0건이라 제외).
COMMUNITY = [
    ("clien.net", "clien", "클리앙"),
    ("ppomppu.co.kr", "ppomppu_web", "뽐뿌"),
    ("dcinside.com", "dcinside", "디시인사이드"),
    ("fmkorea.com", "fmkorea", "에펨코리아"),
    ("theqoo.net", "theqoo", "더쿠"),
    ("teamblind.com", "blind", "블라인드"),
    ("mlbpark.donga.com", "mlbpark", "엠엘비파크"),
    ("bobaedream.co.kr", "bobaedream", "보배드림"),
    ("ruliweb.com", "ruliweb", "루리웹"),
    ("daangn.com", "daangn", "당근 동네생활"),
]


def _community_domains() -> list[tuple[str, str, str]]:
    """GTM_COMMUNITY_DOMAINS(쉼표 구분 도메인)로 좁힐 수 있다. 기본은 위 목록 전부."""
    pick = [d.strip() for d in os.environ.get("GTM_COMMUNITY_DOMAINS", "").split(",") if d.strip()]
    return [c for c in COMMUNITY if not pick or c[0] in pick]


def _board(domain: str, url: str) -> str:
    """URL에 게시판·갤러리 id가 있으면 꺼낸다(표시용)."""
    u = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(u.query)
    if domain == "dcinside.com":
        return (qs.get("id") or [""])[0]
    if domain == "ppomppu.co.kr":
        return (qs.get("id") or [""])[0]
    if domain == "mlbpark.donga.com":
        return (qs.get("b") or [""])[0]
    if domain == "bobaedream.co.kr":
        return (qs.get("code") or [""])[0]
    m = re.search(r"/service/board/([^/]+)/", u.path) if domain == "clien.net" else None
    if m:
        return m.group(1)
    if domain == "theqoo.net":
        seg = [x for x in u.path.split("/") if x]
        return seg[0] if seg and not seg[0].isdigit() else ""
    return ""


def _kst(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(KST).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return iso or ""


def _community(query: str, n: int) -> list[dict]:
    key = config.get("KAKAO_REST_API_KEY")
    if not key:
        raise RuntimeError("KAKAO_REST_API_KEY 없음 — 리블 .env 또는 환경 변수에 둘 것")
    out = []
    for domain, channel, name in _community_domains():
        url = DAUM_API["daum_web"] + "?" + urllib.parse.urlencode(
            {"query": f"site:{domain} {query}", "sort": "recency", "size": max(1, min(n, 10))})
        _budget(_DAUM_CALLS, DAUM_DAILY_CAP, 1, "다음 검색")
        try:
            docs = _get_json(url, {"Authorization": f"KakaoAK {key}"}).get("documents", [])
        except Exception as e:  # noqa: BLE001 — 한 도메인 실패가 나머지를 막지 않게
            _warn(f"community/{domain} 실패({type(e).__name__}) — 이 도메인만 건너뜀")
            continue
        for rank, d in enumerate(docs):
            link = d.get("url", "")
            if domain not in urllib.parse.urlparse(link).netloc:
                continue                      # site: 연산자가 새는 결과는 버린다
            board = _board(domain, link)
            out.append(_item("community", channel=channel, url=link, title=_clean(d.get("title")),
                             desc=_clean(d.get("contents")), where=f"{name}/{board}" if board else name,
                             where_url=f"https://{domain}/", rank=rank, query=query,
                             posted_at=_kst(d.get("datetime", "") or "")))
    return out


# ── 유튜브(YouTube Data API v3) ────────────────────────────────────────────
YT_API = "https://www.googleapis.com/youtube/v3/"
YT_DAILY_UNITS = int(os.environ.get("GTM_YOUTUBE_DAILY_UNITS", "3000"))   # 무료 10,000의 30%
_YT_UNITS = os.environ.get("GTM_YOUTUBE_UNITS", "data/youtube_units.json")
YT_QUESTION = re.compile(r"\?|？|고민|어디|추천|괜찮|궁금|vs|VS|비교")


def _yt_get(endpoint: str, params: dict, cost: int) -> dict:
    """할당량을 먼저 세고 부른다. 403은 이유(API 미사용·키 제한·할당량 초과)를 그대로 올린다."""
    key = (config.get("YOUTUBE_API_KEY") or config.get("GOOGLE_API_KEY"))
    if not key:
        raise RuntimeError("GOOGLE_API_KEY 없음 — 리블 .env 또는 환경 변수에 둘 것")
    _budget(_YT_UNITS, YT_DAILY_UNITS, cost, "유튜브 할당량(단위)")
    url = YT_API + endpoint + "?" + urllib.parse.urlencode({**params, "key": key})
    try:
        return _get_json(url, {})
    except urllib.error.HTTPError as e:
        reason = ""
        try:
            err = json.loads(e.read().decode("utf-8", "replace")).get("error", {})
            reason = (err.get("errors") or [{}])[0].get("reason", "") or err.get("status", "")
        except Exception:  # noqa: BLE001
            pass
        why = {"accessNotConfigured": "YouTube Data API 미사용 설정(프로젝트에서 API를 켜야 함)",
               "SERVICE_DISABLED": "YouTube Data API 미사용 설정(프로젝트에서 API를 켜야 함)",
               "quotaExceeded": "YouTube 일일 할당량 초과",
               "keyInvalid": "API 키가 유효하지 않음",
               "forbidden": "키 제한(리퍼러·IP·API 제한)으로 거부",
               "commentsDisabled": "이 영상은 댓글이 꺼져 있음",
               "API_KEY_SERVICE_BLOCKED": "키 제한으로 YouTube API 차단"}.get(reason, reason or str(e))
        raise RuntimeError(f"YouTube {e.code}: {why}") from None


def _youtube(query: str, n: int) -> list[dict]:
    res = _yt_get("search", {"part": "snippet", "type": "video", "q": query, "regionCode": "KR",
                             "relevanceLanguage": "ko", "order": "date", "maxResults": 5}, 100)
    out = []
    for v in res.get("items", []):
        vid = (v.get("id") or {}).get("videoId")
        sn = v.get("snippet") or {}
        if not vid:
            continue
        try:
            th = _yt_get("commentThreads", {"part": "snippet", "videoId": vid, "maxResults": 20,
                                            "order": "relevance", "textFormat": "plainText"}, 1)
        except RuntimeError as e:
            if "댓글이 꺼져" in str(e):
                continue
            raise
        for t in th.get("items", []):
            top = ((t.get("snippet") or {}).get("topLevelComment") or {})
            c = top.get("snippet") or {}
            body = (c.get("textDisplay") or c.get("textOriginal") or "").strip()
            if not body or not YT_QUESTION.search(body):
                continue
            # 작성자 이름·채널은 저장하지 않는다 — 질문 본문과 위치만.
            out.append(_item("youtube", url=f"https://www.youtube.com/watch?v={vid}&lc={top.get('id', '')}",
                             title=_clean(sn.get("title")), desc=body[:300], where=_clean(sn.get("channelTitle")),
                             where_url=f"https://www.youtube.com/watch?v={vid}", rank=len(out), query=query,
                             posted_at=_kst(c.get("publishedAt", ""))))
            if len(out) >= n:
                return out
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

ALL_PROVIDERS = ["naver_cafe", "naver_kin", "naver_blog", "daum_cafe", "daum_web", "ppomppu",
                 "community", "youtube"]


def available() -> list[str]:
    """키가 있는 provider 전부 + ppomppu(공개 RSS)."""
    out = []
    cid, sec, _ = _naver_keys()
    if cid and sec:
        out += ["naver_cafe", "naver_kin", "naver_blog"]
    if config.get("KAKAO_REST_API_KEY"):
        out += ["daum_cafe", "daum_web", "community"]
    if (config.get("YOUTUBE_API_KEY") or config.get("GOOGLE_API_KEY")):
        out += ["youtube"]
    return out + ["ppomppu"]


def kin_date(url: str) -> str:
    """지식iN 질문 작성일('2026-10-22'). 검색 API가 날짜를 주지 않아 질문 페이지에서 읽는다 — 질문 필터를 통과한 글만 부른다.
    10-24 실측: 날짜순 검색에도 2023~2024년 질문이 섞여 나왔다."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        html = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", "ignore")
    except Exception:
        return ""
    m = re.search(r"작성일</span>\s*(\d{4})\.(\d{2})\.(\d{2})", html)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


def search(query: str, channel: str = "naver_cafe", n: int = 20) -> list[dict]:
    """한 provider·한 질의. 실패하면 예외를 그대로 올린다(하위 호환)."""
    if channel in NAVER_API:
        return _naver(channel, query, n)
    if channel in DAUM_API:
        return _daum(channel, query, n)
    if channel == "ppomppu":
        return _ppomppu_match(_ppomppu_feed(), query)[:n]
    if channel == "community":
        return _community(query, n)
    if channel == "youtube":
        return _youtube(query, n)
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
