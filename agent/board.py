"""게시판 스캐너 — 가입한 카페의 최신·인기 글을 읽고 답변 후보를 만든다. 게시하지 않는다(게시는 사람 승인 뒤 poster).

흐름(글마다 동시 처리):  목록(Aside 로그인 탭, 읽기만) → 질문 신호·광고 신호·게시판 거름 → 원문 읽기(reader) →
  Haiku 판정 → Sonnet 추출(단지별 targets) → 답변 유형 선택 → 초안(Sonnet) → 최종 검수(Opus·Sonnet) → 질문 적합성 관문(Opus 채점)

답변 유형(10-30 확장)
  price  가격형   — 글쓴이가 말한 평형 기준 최근 실거래로 답(비교 질문의 기본)
  link   링크형   — 단지가 하나라도 확정되면 그 단지 페이지, 리블이 가진 사실(세대수·연식·학군/역세권 점수·관리비·전세가율) 한두 개
  hub    허브형   — 단지가 없고 학교·역·지역을 말한 글이면 그 허브 페이지
  학군·통근·재건축·전세가 고민이면 링크형을 먼저, 아니면 가격형을 먼저 시도하고, 검수나 관문에서 떨어지면 다른 유형으로 한 번 더 시도한다.

사용:  PYTHONPATH=. python -m agent.board --cafe jaegebal --cafe rainup --hours 72 --workers 6
카페 번호(clubid)는 공개 CafeGateInfo로 읽어 data/cafe_ids.json(저장소 제외)에 둔다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from . import claude, intent, judge, queue, reader, reply
from .pipeline import AD
from .poster import EXE

# 카페별 표시 이름(심사 문맥)과 거를 게시판. 이름은 공개된 카페 이름이다.
CAFES = {
    "jaegebal": {"name": "부동산 스터디'", "boards": {"▶회원간 묻고 답하기", "▶내집 마련 상담", "§부동산스터디(통합)§", "▶내가 투자하고 싶은 부동산"},
                 "board_suffix": "부동산"},
}
BAD_BOARD = re.compile(r"법무법인|추천인|교환|구해요|팔아요|이벤트|응모|후기|가입인사|뉴스|News|Hot Issue|일기|공지|출석|매매일지|인증|광고|홍보|자유이야기|자유게시판|자유 게시판|자유 토크|와글|잡담")
LINK_FIRST = {"school", "commute", "rebuild", "jeonse"}

_LIST_JS = """
const R = (o) => console.log('RESULT ' + JSON.stringify(o));
const p = await openTab('https://m.cafe.naver.com/');
await sleep(2500);
const out = await p.evaluate(async ([clubs, PAGES]) => {
  const res = [];
  for (const [slug, id] of clubs) for (let pg = 1; pg <= PAGES; pg++) {
    await new Promise(f => setTimeout(f, 1400));
    try {
      const j = await (await fetch(`https://apis.naver.com/cafe-web/cafe2/ArticleListV2dot1.json?search.clubid=${id}&search.queryType=lastArticle&search.perPage=50&search.page=${pg}`, {credentials: 'include'})).json();
      for (const a of (j.message.result.articleList || [])) res.push({slug, id: a.articleId, menu: a.menuName, s: a.subject, rc: a.readCount, cc: a.commentCount, t: a.writeDateTimestamp, blind: a.blindArticle, cm: a.enableComment});
    } catch (e) { res.push({slug, err: String(e).slice(0, 80)}); }
  }
  return res;
}, [CLUBS, PAGES]);
console.log('DATA ' + JSON.stringify(out));
R({n: out.length});
try { await p.close(); } catch (e) {}
"""


def fetch_lists(slugs: list[str], pages: int = 6, timeout: int = 240) -> list[dict]:
    """카페 최신 글 목록(읽기만). 한 번에 30초 안에 끝나도록 카페×쪽 수를 약 18쪽씩 나눠 읽는다."""
    clubs = [(s, reader.cafe_id(s)) for s in slugs]
    clubs = [(s, i) for s, i in clubs if i]
    per = max(1, 18 // pages)
    out = []
    for i in range(0, len(clubs), per):
        js = f"const PAGES={pages}; const CLUBS={json.dumps(clubs[i:i + per])};" + _LIST_JS
        r = subprocess.run([EXE, "repl", js], capture_output=True, text=True, timeout=timeout)
        for ln in ((r.stdout or "") + (r.stderr or "")).split("\n"):
            if ln.startswith("DATA "):
                out += [a for a in json.loads(ln[5:]) if "id" in a]
    return out


def candidates(arts: list[dict], hours: float, seen: set[str]) -> list[dict]:
    now, out = time.time() * 1000, []
    for a in arts:
        cfg = CAFES.get(a["slug"], {})
        m = a["menu"]
        if cfg.get("boards"):
            if not (m in cfg["boards"] or (m.endswith(cfg.get("board_suffix", "\0")) and not m.startswith(("▶", "♨")))):
                continue
        elif BAD_BOARD.search(m):
            continue
        if a.get("blind") or a.get("cm") is False or AD.search(a["s"]):
            continue
        a["h"] = (now - a["t"]) / 3.6e6
        a["hot"] = (a["cc"] * 3 + a["rc"] / 20) / (a["h"] + 2)             # 인기 점수 — 댓글·조회가 많고 최근일수록 높다
        a["url"] = f"https://cafe.naver.com/{a['slug']}/{a['id']}"
        if a["h"] <= hours and a["url"] not in seen:
            out.append(a)
    return sorted(out, key=lambda a: -a["hot"])


def _try(kind: str, text: str, ents: dict, post: dict, comments: list[str], res: dict) -> tuple[dict | None, dict, float]:
    """한 유형으로 초안 → 검수 → 관문. 통과하면 ({text, src, review, grade}, last_review, 비용)."""
    spent, last = 0.0, {}
    cs = []
    try:
        dr, c = judge.draft({"title": post["title"], "text": post["text"]}, ents or {}, res.get("facts", []), "naver_cafe",
                            res["links"][0], intent.style("naver_cafe"), kind); spent += c
        if dr.get("text"):
            cs.append(("sonnet", dr["text"]))
    except claude.BudgetExceeded:
        raise
    except Exception:
        pass
    cs.append(("rule", res["text"]))
    for src, txt in cs:
        rev, c = judge.audit(post, comments, txt, res.get("facts", []), res.get("complexes", []), res["links"][0],
                             "naver_cafe", intent.style("naver_cafe"), kind); spent += c
        last = rev
        if rev.get("pass"):
            ok, g, c2 = judge.relevance_gate(post, comments, txt); spent += c2
            last = {**rev, "grade": g, "relevance_ok": ok}
            if ok:
                return {"text": txt, "src": src, "review": rev, "grade": g}, last, spent
            break                                  # 질문에 닿지 못한 답은 다른 초안으로 고치지 않고 유형을 바꾼다
        if rev.get("by") != "code" and set(rev.get("failed", [])) - {"facts_only", "honest_tone"}:
            break
    return None, last, spent


def process(a: dict, art: dict) -> dict:
    cfg = CAFES.get(a["slug"], {})
    where = cfg.get("name") or a["slug"]
    rec = {"slug": a["slug"], "id": a["id"], "url": a["url"], "menu": a["menu"], "title": a["s"], "rc": a["rc"], "cc": a["cc"],
           "h": round(a["h"], 1), "hot": round(a["hot"], 2), "spent": 0.0}

    def fin(status, reason="", **kv):
        rec.update(status=status, reason=reason, **kv)
        return rec

    if not art.get("ok"):
        return fin("skip", "deleted" if art.get("deleted") else "unread", why=str(art.get("reason", ""))[:80])
    if not art.get("can_comment"):
        return fin("skip", "no_permission", why=str(art.get("no_comment_why", "")))
    if any("reevl" in c or "리블" in c for c in art.get("comments") or []):
        return fin("skip", "already_linked")
    title, body = art.get("subject") or a["s"], art.get("text") or ""
    comments = art.get("comments") or []
    spent = 0.0
    try:
        tri, c = judge.triage(title, body[:400], "naver_cafe", where, {}); spent += c
        if not (tri.get("is_question") and tri.get("appropriate")):
            return fin("skip", "ai_reject", why=tri.get("reason", "")[:120], spent=spent)
        ents, c = judge.extract(title, body); spent += c
        text = f"{title}\n{body}"
        post = {"board": art.get("board", ""), "title": title, "text": body, "written": art.get("written", "")}
        routes = ["link", "price"] if ents.get("concern") in LINK_FIRST else ["price", "link"]
        tried, last, pending = [], {}, None
        for kind in routes:
            res = reply.build(text, "naver_cafe", f"naver_cafe-{a['slug']}-{a['id']}", False, entities=ents) if kind == "price" \
                else reply.build_link(text, ents)
            if not res.get("ok"):
                tried.append(f"{kind}: {res.get('why', '')[:50]}"); continue
            kind = res.get("kind", kind)
            ok, last, c = _try(kind, text, ents, post, comments, res); spent += c
            if ok:
                return fin("ready", draft=ok["text"], link=res["links"][0], spent=spent, facts=res.get("facts", []), kind=kind,
                           relevance=ok["grade"].get("relevance"), grade=ok["grade"], comments_now=a["cc"],
                           ai_review=f"통과({kind}·{ok['src']} · {ok['review'].get('by')} · {ok['review'].get('confidence', 0):.2f} · 적합성 {ok['grade'].get('relevance')}/5)",
                           why=f"{where} 게시판 직접 탐색 · 검수·관문 통과")
            pending = pending or (kind, res, last)
            tried.append(f"{kind}: " + ("; ".join(last.get("issues", []))[:90] or ("적합성 " + str((last.get("grade") or {}).get("relevance")) + "/5")))
        if not pending:
            return fin("skip", "no_match", why=" | ".join(tried)[:200], spent=spent, ents=[t.get("name") for t in ents.get("targets", [])])
        kind, res, last = pending
        hard = set(last.get("failed", [])) & {"live_question", "right_target", "not_redundant", "fits_board"}
        return fin("skip" if hard else "needs_human", ("audit_" + sorted(hard)[0]) if hard else "", draft="", link=res["links"][0], spent=spent,
                   ai_review=" | ".join(tried)[:300], why="검수·관문 걸림 — " + " | ".join(tried)[:150], kind=kind)
    except claude.BudgetExceeded as e:
        return fin("skip", "budget", why=str(e)[:80], spent=spent)
    except Exception as e:
        return fin("skip", "error", why=f"{type(e).__name__}: {str(e)[:80]}", spent=spent)


RETRY_REASONS = {"no_match", "audit_right_target", "audit_live_question", "audit_answers_ask"}


def run(slugs: list[str], hours: float = 72, pages: int = 6, workers: int = 6, write: bool = True, retry: bool = False) -> list[dict]:
    arts = fetch_lists(slugs, pages)
    rows = queue.load()
    # --retry: 매칭 실패·검수 탈락·사람 확인으로 끝난 글을 새 로직으로 다시 본다(게시된 글·AI 판정 제외 글은 건드리지 않는다)
    redo = {r["source_url"]: r for r in rows if retry and (r["status"] == "needs_human" or (r["status"] == "skip" and r["removal_reason"] in RETRY_REASONS))}
    seen = {r["source_url"] for r in rows} - set(redo)
    cands = candidates(arts, hours, seen)
    print(f"목록 {len(arts)}글 → 후보 {len(cands)}", flush=True)
    read = reader.read_many([("naver_cafe", a["url"]) for a in cands])

    def one(a):
        r = process(a, read.get(a["url"]) or {})
        print(r["status"], r.get("reason", ""), a["slug"], a["id"], a["s"][:36], flush=True)
        return r

    with ThreadPoolExecutor(workers) as ex:
        out = list(ex.map(one, cands))
    if write:
        for r in out:
            old = redo.get(r["url"])
            fields = {"status": r["status"], "removal_reason": r["reason"] if r["status"] == "skip" else "", "why": r.get("why", ""),
                      "draft": r.get("draft", ""), "link": r.get("link", ""), "ai_review": r.get("ai_review", "")}
            if old:
                queue.mark(old["candidate_id"], **fields)
                continue
            queue.append({"channel": "naver_cafe", "source_url": r["url"], "title": r["title"], "where": (CAFES.get(r["slug"]) or {}).get("name") or r["slug"],
                          "query": f"board:{r['slug']}:{r['menu']}", "provider": "cafe_board", "intent": r.get("kind") or "compare",
                          "status": r["status"], "removal_reason": r["reason"] if r["status"] == "skip" else "", "why": r.get("why", ""),
                          "draft": r.get("draft", ""), "link": r.get("link", ""), "ai_review": r.get("ai_review", ""), "ai_cost": round(r.get("spent", 0), 6),
                          "draft_hash": hashlib.sha1(r.get("draft", "").encode()).hexdigest()[:12] if r.get("draft") else ""})
    print("요약", dict(Counter((r["status"], r.get("reason", "")) for r in out)), "비용 $%.3f" % sum(r.get("spent", 0) for r in out))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="가입한 카페 게시판 스캔 → 답변 후보(게시 안 함)")
    ap.add_argument("--cafe", action="append", required=True, help="카페 주소(slug). 여러 번 줄 수 있다")
    ap.add_argument("--hours", type=float, default=72)
    ap.add_argument("--pages", type=int, default=6)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--dry", action="store_true", help="큐에 쓰지 않는다")
    ap.add_argument("--retry", action="store_true", help="매칭 실패·검수 탈락·사람 확인 글을 새 로직으로 다시 본다")
    a = ap.parse_args(argv)
    run(a.cafe, a.hours, a.pages, a.workers, write=not a.dry, retry=a.retry)
    return 0


if __name__ == "__main__":
    sys.exit(main())
