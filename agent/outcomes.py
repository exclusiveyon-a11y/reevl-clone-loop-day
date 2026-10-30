"""게시 결과 추적 — 올린 댓글이 살아 있는가, 우리 댓글에 답글이 달렸는가, 원글 작성자가 반응했는가. 읽기만 한다.

세는 것(분자/분모를 함께 낸다 — 표본이 작다):
  유지율      원글과 우리 댓글이 그대로 있는 게시물 / 전체 게시물
  대화 반응률  우리 댓글에 답글이 달린 게시물 / 살아 있는 게시물
  질문자 반응률 그중 원글 작성자가 답글을 단 게시물 / 살아 있는 게시물
  24시간 이상 지난 게시물만 따로 센다(방금 올린 글은 아직 반응할 시간이 없다)
카페 글 댓글 API: apis.naver.com/cafe-web/cafe-articleapi/v2/cafes/{id}/articles/{n}/comments/pages/1 — 답글은 isRef=true·refId=부모 댓글,
원글 작성자는 isArticleWriter. 닉네임·답글 본문은 저장하지 않고 개수와 길이만 남긴다.
사용:  PYTHONPATH=. python -m agent.outcomes [--json data/outcomes.json]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import datetime

from . import reader
from .poster import EXE
from .queue import KST, load

_JS = """
const R = (o) => console.log('RESULT ' + JSON.stringify(o));
const p = await openTab('https://m.cafe.naver.com/');
await sleep(2500);
const out = await p.evaluate(async (items) => {
  const res = [];
  for (const [cafeId, id] of items) {
    const row = {cafeId, id};
    try {
      const r = await fetch(`https://apis.naver.com/cafe-web/cafe-articleapi/v2.1/cafes/${cafeId}/articles/${id}?useCafeId=true`, {credentials: 'include'});
      const j = await r.json();
      if (!r.ok) { row.alive = false; row.err = String(j?.result?.errorCode || r.status); res.push(row); continue; }
      row.alive = true; row.cc = j.result.article?.commentCount || 0;
    } catch (e) { row.err = String(e).slice(0, 60); res.push(row); continue; }
    try {
      const r2 = await fetch(`https://apis.naver.com/cafe-web/cafe-articleapi/v2/cafes/${cafeId}/articles/${id}/comments/pages/1?requestFrom=A&orderBy=asc&pageSize=100`, {credentials: 'include'});
      const j2 = await r2.json();
      const items2 = j2?.result?.comments?.items || [];
      row.cmts = items2.map(c => ({id: c.id, ref: c.refId, rep: !!c.isRef, w: !!c.isArticleWriter, del: !!c.isDeleted, link: /reevl\\.co\\.kr\\/c\\//.test(c.content || ''), n: (c.content || '').length, t: c.updateDate}));
    } catch (e) { row.err2 = String(e).slice(0, 60); }
    res.push(row);
    await new Promise(f => setTimeout(f, 1300));
  }
  return res;
}, ITEMS);
console.log('DATA ' + JSON.stringify(out));
R({n: out.length});
try { await p.close(); } catch (e) {}
"""


def _key(url: str) -> tuple[str, int] | None:
    m = re.search(r"cafe\.naver\.com/([A-Za-z0-9_-]+)/(\d+)", url or "")
    return (m.group(1), int(m.group(2))) if m else None


def collect(rows: list[dict] | None = None, now: datetime | None = None) -> dict:
    now = now or datetime.now(KST)
    rows = [r for r in (rows or load()) if r["status"] == "posted" and _key(r["source_url"])]
    items = []
    for r in rows:
        slug, aid = _key(r["source_url"])
        cid = reader.cafe_id(slug)
        if cid:
            items.append([cid, aid])
    got = []
    for i in range(0, len(items), 6):                       # 한 번에 30초 안에 끝나게 6건씩
        out = subprocess.run([EXE, "repl", f"const ITEMS={json.dumps(items[i:i + 6])};" + _JS], capture_output=True, text=True, timeout=240)
        for ln in ((out.stdout or "") + (out.stderr or "")).split("\n"):
            if ln.startswith("DATA "):
                got += json.loads(ln[5:])
    by = {(g["cafeId"], g["id"]): g for g in got}
    per = []
    for r in rows:
        slug, aid = _key(r["source_url"])
        g = by.get((reader.cafe_id(slug), aid)) or {}
        c = g.get("cmts") or []
        ours = [x for x in c if x["link"] and not x["rep"] and not x["del"]]
        reps = [x for x in c if x["rep"] and ours and x["ref"] == ours[0]["id"]]
        age_h = None
        if len(r["posted_at"] or "") >= 16:
            age_h = (now - datetime.strptime(r["posted_at"][:16], "%Y-%m-%d %H:%M").replace(tzinfo=KST)).total_seconds() / 3600
        per.append({"candidate_id": r["candidate_id"], "posted_at": r["posted_at"], "age_h": None if age_h is None else round(age_h, 1),
                    "post_alive": bool(g.get("alive")), "our_comment_alive": bool(ours), "replies": len(reps),
                    "author_replies": sum(1 for x in reps if x["w"]), "comments_total": g.get("cc"), "err": g.get("err") or g.get("err2")})
    # 발견 → 게시까지 걸린 시간(큐의 found_at·posted_at, 둘 다 시각이 있는 행만). 사람 승인 대기를 포함한 값이라 '사람이 쓴 시간'은 아니다.
    lat = []
    for r in rows:
        if len(r["found_at"] or "") >= 16 and len(r["posted_at"] or "") >= 16:
            try:
                t0 = datetime.strptime(r["found_at"][:16], "%Y-%m-%d %H:%M")
                t1 = datetime.strptime(r["posted_at"][:16], "%Y-%m-%d %H:%M")
                lat.append((t1 - t0).total_seconds() / 3600)
            except ValueError:
                pass
    lat.sort()
    n = len(per)
    live = [p for p in per if p["our_comment_alive"]]
    old = [p for p in live if (p["age_h"] or 0) >= 24]
    agg = {"posted": n, "kept": len(live), "with_reply": sum(1 for p in live if p["replies"]), "author_reacted": sum(1 for p in live if p["author_replies"]),
           "older24_live": len(old), "older24_author_reacted": sum(1 for p in old if p["author_replies"]),
           "deleted_posts": sum(1 for p in per if not p["post_alive"]),
           "found_to_post_h_median": round(lat[len(lat) // 2], 1) if lat else None, "found_to_post_n": len(lat)}
    return {"aggregate": agg, "per_post": per}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    res = collect()
    g = res["aggregate"]
    print(f"게시 {g['posted']}건 · 유지 {g['kept']}/{g['posted']} · 삭제된 원글 {g['deleted_posts']} · 대화 반응 {g['with_reply']}/{g['kept']} · "
          f"질문자 반응 {g['author_reacted']}/{g['kept']} · (24시간 이상 지난 글만: 질문자 반응 {g['older24_author_reacted']}/{g['older24_live']}) · "
          f"발견→게시 중앙값 {g['found_to_post_h_median']}시간(n={g['found_to_post_n']})")
    if a.json:
        json.dump(res, open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
