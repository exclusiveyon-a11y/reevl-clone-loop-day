"""사용법 (python -m agent)
  reply   --text "오금동 삼성이랑 상아2차 고민" [--file post.txt] [--channel naver_cafe] [--placement p] [--qa] [--json]
  compare "11710|삼성|오금동|160" "11710|상아2차아파트|오금동|165" [--channel copy] [--qa]
  discover --query "오금 삼성 상아" [--provider naver ...]
  run     --query "송파 리모델링 비교" [--query ...] | --keywords-file data/keywords.txt
          [--provider naver ...] [--publish] [--cap 3] [--qa]
  queue list | queue mark <id> --status posted --posted-url <URL> [--removal-reason ...]
  human   사람이 게시해야 할 후보(needs_human·ready)를 URL·초안과 함께 출력
테스트 링크는 --qa(utm_campaign=gtm_qa)로 만들어 실험 집계와 섞지 않는다.
--publish 없이 돌리면 게시하지 않는다(초안·큐까지만).
"""
from __future__ import annotations

import argparse
import json
import sys

from . import draft, pipeline, queue, reply
from .links import SOURCES


def _queries(a) -> list[str]:
    qs = list(a.query or [])
    if a.keywords_file:
        qs += [l.strip() for l in open(a.keywords_file, encoding="utf-8") if l.strip() and not l.startswith("#")]
    return qs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m agent")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("reply", help="게시글 → 리블 링크 달린 답글 초안")
    r.add_argument("--text"); r.add_argument("--file")
    r.add_argument("--channel", default="copy", choices=sorted(SOURCES))
    r.add_argument("--placement"); r.add_argument("--qa", action="store_true"); r.add_argument("--json", action="store_true")

    c = sub.add_parser("compare", help="단지 cid 2~3개 → 비교 근거 초안")
    c.add_argument("cids", nargs="+"); c.add_argument("--channel", default="copy", choices=sorted(SOURCES))
    c.add_argument("--placement"); c.add_argument("--qa", action="store_true")

    d = sub.add_parser("discover", help="검색 API로 후보만 찾기")
    d.add_argument("--query", action="append", required=True); d.add_argument("--provider", action="append")

    rn = sub.add_parser("run", help="찾기→초안→점수→큐→(허용 시)게시→실패 시 사람")
    rn.add_argument("--query", action="append"); rn.add_argument("--keywords-file")
    rn.add_argument("--provider", action="append"); rn.add_argument("--publish", action="store_true")
    rn.add_argument("--cap", type=int, default=3); rn.add_argument("--rules", default="data/rules.json")
    rn.add_argument("--qa", action="store_true")

    q = sub.add_parser("queue", help="수요 후보 큐")
    qs = q.add_subparsers(dest="qcmd", required=True)
    qs.add_parser("list")
    qm = qs.add_parser("mark"); qm.add_argument("id"); qm.add_argument("--status"); qm.add_argument("--posted-url")
    qm.add_argument("--removal-reason")

    sub.add_parser("human", help="사람이 게시할 목록")
    a = ap.parse_args(argv)

    if a.cmd == "reply":
        text = a.text or (open(a.file, encoding="utf-8").read() if a.file else sys.stdin.read())
        res = reply.build(text, a.channel, a.placement, a.qa)
        if a.json:
            print(json.dumps(res, ensure_ascii=False, indent=1))
        else:
            print(res["text"] if res["ok"] else "⚠ " + res["why"])
        return 0 if res["ok"] else 2
    if a.cmd == "compare":
        print(draft.build(a.cids, a.channel, a.placement, qa=a.qa)["text"])
        return 0
    if a.cmd == "discover":
        for it in pipeline._search(a.query, a.provider):
            print(f"{it.get('provider', '')}\t{it.get('channel')}\t{it.get('rank')}\t{it.get('where')}\t{it.get('title')}\t{it.get('url')}")
        return 0
    if a.cmd == "run":
        qs_ = _queries(a)
        if not qs_:
            ap.error("--query 또는 --keywords-file 필요")
        print(json.dumps(pipeline.run(qs_, a.provider, a.publish, a.cap, a.rules, a.qa), ensure_ascii=False))
        return 0
    if a.cmd == "human":
        rows = [r_ for r_ in queue.load() if r_["status"] in ("needs_human", "ready")]
        if not rows:
            print("사람이 게시할 후보가 없습니다.")
        for r_ in rows:
            print(f"── {r_['candidate_id']} · {r_['status']} · {r_['channel']} · {r_['where']} · 점수 {r_['score']}")
            print(f"글: {r_['source_url']}\n사유: {r_['why']}\n{r_['draft']}\n"
                  f"게시 후: python -m agent queue mark {r_['candidate_id']} --status posted --posted-url <URL>\n")
        return 0
    if a.qcmd == "list":
        for r_ in queue.load():
            print(r_["candidate_id"], r_["status"], r_["score"], r_["channel"], r_["where"], r_["placement_id"], r_["title"][:40])
    elif a.qcmd == "mark":
        kv = {k: v for k, v in {"status": a.status, "posted_url": a.posted_url, "removal_reason": a.removal_reason}.items() if v}
        if a.status == "posted":
            from datetime import datetime
            kv["posted_at"] = datetime.now(queue.KST).strftime("%Y-%m-%d %H:%M")
        print(queue.mark(a.id, **kv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
