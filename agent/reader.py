"""원문 읽기 — 검수가 검색 요약(약 200자)이 아니라 글 전체·게시판·기존 댓글을 보고 판단하게 한다.

- 네이버 카페: Aside(운영자 로그인 세션) 탭 하나에서 카페 글 API(apis.naver.com/cafe-web/cafe-articleapi/v2.1)를 차례로 읽는다.
  게시판 이름·작성 시각·본문·댓글·댓글 쓰기 권한(authority.isWriteComment)이 한 번에 온다 → 미가입·등급 제한·삭제 글을 게시 전에 거른다.
  10-25 실측: 10-24에 댓글을 단 부동산스터디 글이 4003(삭제되었거나 존재하지 않는 게시글)로 나왔다 — 게시 뒤 생존 확인에도 쓴다.
- 네이버 블로그: 공개 글이라 HTTP로 본문만 읽는다(댓글 칸은 게시 직전 dry-run이 확인).
- 원문은 로컬 캐시(data/article_cache.json, 저장소 제외)에만 둔다. 작성자·댓글 닉네임은 담지 않는다.
"""
from __future__ import annotations

import html as _html
import json
import os
import re
import subprocess
import time
import urllib.request
from datetime import datetime

from .poster import EXE
from .queue import KST

CACHE = os.environ.get("GTM_ARTICLE_CACHE", "data/article_cache.json")
IDS = os.environ.get("GTM_CAFE_IDS", "data/cafe_ids.json")
TTL = 3 * 3600
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"


def _load(path: str) -> dict:
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return {}


def _save(path: str, d: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        json.dump(d, open(path, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass


def cafe_id(slug: str) -> int | None:
    """카페 주소(jaegebal) → 숫자 id. 공개 CafeGateInfo로 한 번 묻고 로컬에 둔다."""
    ids = _load(IDS)
    if slug in ids:
        return ids[slug]
    try:
        req = urllib.request.Request(f"https://apis.naver.com/cafe-web/cafe2/CafeGateInfo.json?cluburl={slug}",
                                     headers={"User-Agent": UA})
        d = json.load(urllib.request.urlopen(req, timeout=10))
        cid = int(d["message"]["result"]["cafeInfoView"]["cafeId"])
    except Exception:
        return None
    ids[slug] = cid
    _save(IDS, ids)
    return cid


def _cafe_key(url: str) -> tuple[int, int] | None:
    m = re.search(r"cafes/(\d+)/articles/(\d+)", url)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"cafe\.naver\.com/([A-Za-z0-9_-]+)/(\d+)", url)
    if m:
        cid = cafe_id(m.group(1))
        return (cid, int(m.group(2))) if cid else None
    return None


_JS = """
const ITEMS = %s;
const R = (o) => console.log('RESULT ' + JSON.stringify(o));
const p = await openTab('https://m.cafe.naver.com/');
await sleep(2500);
let out = [];
try {
  out = await p.evaluate(async (items) => {
    const plain = (h) => {
      const s = (h || '').replace(/<\\/(p|div|li|h\\d)>/gi, '\\n').replace(/<br\\s*\\/?>/gi, '\\n');
      const t = new DOMParser().parseFromString(s, 'text/html').body.textContent || '';
      return t.split('\\n').map(x => x.replace(/[\\s\\u200b]+/g, ' ').trim()).filter(Boolean).join('\\n');
    };
    const res = [];
    for (const [url, cafeId, articleId] of items) {
      try {
        const r = await fetch(`https://apis.naver.com/cafe-web/cafe-articleapi/v2.1/cafes/${cafeId}/articles/${articleId}?useCafeId=true`,
                              {credentials: 'include'});
        const j = await r.json();
        if (!r.ok) { res.push({url, ok: false, status: r.status, code: j?.result?.errorCode || '', reason: (j?.result?.reason || '').slice(0, 80)}); }
        else {
          const x = j.result || {}, a = x.article || {}, au = x.authority || {}, cm = x.comments || {};
          res.push({url, ok: true, subject: a.subject || '', board: a.menu?.name || '', written: a.writeDate || 0,
                    comment_count: a.commentCount || 0, text: plain(a.contentHtml).slice(0, 3000),
                    comments: (cm.items || []).filter(c => !c.isDeleted).slice(0, 15).map(c => (c.content || '').replace(/\\s+/g, ' ').slice(0, 200)),
                    can_comment: !!(au.isWriteComment && a.isWriteComment !== false), no_comment_why: cm.disableWriteReason || '',
                    blind: !!a.isBlind});
        }
      } catch (e) { res.push({url, ok: false, status: 0, reason: String(e).slice(0, 80)}); }
      await new Promise(f => setTimeout(f, 1200));     // 카페 API를 몰아서 부르지 않는다
    }
    return res;
  }, ITEMS);
} catch (e) { out = [{ok: false, reason: 'evaluate ' + String(e).slice(0, 80)}]; }
try { await p.close(); } catch (e) {}
// ★글마다 한 줄 — 20건을 한 줄 JSON으로 내보냈더니 출력이 잘려 묶음 전체가 '결과 없음'이 됐다(10-25)
for (const o of out) console.log('ITEM ' + JSON.stringify(o));
R({n: out.length});
"""


def cafe(urls: list[str], timeout: int = 240) -> dict[str, dict]:
    """카페 글 여러 개를 탭 하나로 읽는다. {url: info}. 실패한 글은 ok=False와 이유."""
    out, items = {}, []
    for u in urls:
        k = _cafe_key(u)
        if k:
            items.append([u, k[0], k[1]])
        else:
            out[u] = {"ok": False, "reason": "카페 글 주소를 해석하지 못함"}
    for i in range(0, len(items), 20):
        chunk = items[i:i + 20]
        got, tail = [], ""
        for _ in range(2):                       # Aside가 가끔 빈 결과를 낸다(10-25: 같은 묶음이 다시 돌리면 읽혔다) — 한 번 더
            try:
                r = subprocess.run([EXE, "repl", _JS % json.dumps(chunk)], capture_output=True, text=True, timeout=timeout)
                out_ = (r.stdout or "") + (r.stderr or "")
                tail = out_[-160:]
                # ★str.splitlines()는 본문 속 U+2028도 줄바꿈으로 본다 — '\n'으로만 나눈다
                for ln in out_.split("\n"):
                    if ln.startswith("ITEM "):
                        try:
                            got.append(json.loads(ln[5:]))
                        except ValueError:
                            pass
            except Exception as e:
                tail = f"Aside {type(e).__name__}"
            if got:
                break
        for g in got:
            if g.get("url"):
                if g.get("written"):
                    g["written"] = datetime.fromtimestamp(g["written"] / 1000, KST).strftime("%Y-%m-%d %H:%M")
                g["deleted"] = (not g.get("ok")) and g.get("code") == "4003"
                out[g["url"]] = g
        for u, _, _ in chunk:
            out.setdefault(u, {"ok": False, "reason": "Aside 결과 없음(데몬·로그인 확인) " + tail.strip()[-80:]})
    return out


def blog(url: str) -> dict:
    """공개 블로그 글 본문. 댓글은 읽지 않는다."""
    m = re.search(r"blog\.naver\.com/([^/?#]+)/(\d+)", url) or re.search(r"blogId=([^&]+).*?logNo=(\d+)", url)
    if not m:
        return {"ok": False, "reason": "블로그 주소를 해석하지 못함"}
    try:
        req = urllib.request.Request(f"https://blog.naver.com/PostView.naver?blogId={m.group(1)}&logNo={m.group(2)}",
                                     headers={"User-Agent": UA})
        h = urllib.request.urlopen(req, timeout=12).read().decode("utf-8", "ignore")
    except Exception as e:
        return {"ok": False, "reason": type(e).__name__}
    body = h.split('class="se-main-container"', 1)[-1] if 'se-main-container' in h else h
    body = re.sub(r"<script.*?</script>|<style.*?</style>", " ", body, flags=re.S)
    body = re.sub(r"</(p|div|li)>|<br\s*/?>", "\n", body)
    txt = _html.unescape(re.sub(r"<[^>]+>", " ", body))
    lines = [re.sub(r"[\s\u200b]+", " ", ln).strip() for ln in txt.split("\n")]
    title = re.search(r'<meta property="og:title" content="([^"]*)"', h)
    return {"ok": True, "subject": _html.unescape(title.group(1)) if title else "", "board": "", "written": "",
            "text": "\n".join(ln for ln in lines if ln)[:3000], "comments": [], "can_comment": None}


def read_many(items: list[tuple[str, str]]) -> dict[str, dict]:
    """[(channel, url)] → {url: info}. 3시간 캐시. 카페는 한 번에 묶어 읽는다."""
    cache, now = _load(CACHE), time.time()
    out, need_cafe = {}, []
    for ch, u in items:
        hit = cache.get(u)
        if hit and now - hit.get("_t", 0) < TTL:
            out[u] = hit
        elif ch == "naver_cafe":
            need_cafe.append(u)
        elif ch == "naver_blog":
            out[u] = blog(u)
        else:
            out[u] = {"ok": False, "reason": f"{ch} 원문 읽기 미지원"}
    if need_cafe:
        out.update(cafe(need_cafe))
    for u, v in out.items():
        if v.get("ok") or v.get("deleted"):
            cache[u] = {**v, "_t": now}
    _save(CACHE, cache)
    return out


def read(channel: str, url: str) -> dict:
    return read_many([(channel, url)]).get(url, {"ok": False, "reason": "읽기 실패"})
