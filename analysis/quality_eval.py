"""게시한 댓글의 사후 품질 평가 — 6개 항목(1~5점)을 Claude(Opus)가 채점한다.

게시 전 검수(judge.audit)와 별개다. 같은 계열 모델이 채점하므로 독립 검증은 아니고, 약점을 찾는 내부 점검용이다.
실행: PYTHONPATH=. python analysis/quality_eval.py [--out data/quality.json]
원문·댓글은 로컬 캐시(data/article_cache.json)와 카페 읽기에서만 가져오고 저장소에 남기지 않는다. 출력은 로컬 파일과 화면.
"""
from __future__ import annotations

import argparse
import csv
import json

from agent import judge, reader

ITEMS = judge.GRADE_ITEMS


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/quality.json")
    ap.add_argument("--queue", default="data/queue.csv")
    a = ap.parse_args()
    rows = [r for r in csv.DictReader(open(a.queue, encoding="utf-8")) if r["status"] == "posted"]
    cache = json.load(open("data/article_cache.json", encoding="utf-8"))
    out, cost = [], 0.0
    for r in rows:
        u = r["source_url"]
        art = cache.get(u) or {}
        if not art.get("text"):
            out.append({"id": r["candidate_id"], "skipped": "원문 캐시 없음(원글 삭제 등)"}); continue
        live = reader.read_many([("naver_cafe", u)]).get(u, {})
        others = [c[:160] for c in (live.get("comments") or []) if "reevl" not in c][:12]
        v, usd = judge.grade({"title": art.get("subject") or r["title"], "text": art["text"], "board": art.get("board", "")}, others, r["draft"])
        cost += usd
        if not v:
            out.append({"id": r["candidate_id"], "skipped": "채점 형식 불량"}); continue
        out.append({"id": r["candidate_id"], **v})
        print(r["candidate_id"], {k: v[k] for k in ITEMS}, flush=True)
    json.dump(out, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    ok = [o for o in out if "relevance" in o]
    if ok:
        print(f"n={len(ok)}", {k: round(sum(o[k] for o in ok) / len(ok), 2) for k in ITEMS}, "비용 $%.3f" % cost)
        print("약점:", *[f"- {o['weakness']}" for o in ok][:11], sep="\n")


if __name__ == "__main__":
    main()
