"""검수 평가 — 답을 아는 사례(떨어져야 할 답글 · 통과해야 할 답글)로 검수기를 잰다.

사례 파일은 data/review_eval.json(저장소 제외 — 커뮤니티 원문이 들어 있다). 한 사례:
  {id, label: "bad"|"good", expect: [떨어져야 할 검사 이름], note, channel, post{board,title,text,written},
   comments[], draft, facts[], complexes[{cid,name,gu,dong,hh}], link}
결과는 data/review_eval_result.json. 문서에는 집계만 옮긴다.

  python -m agent.review_eval            # 새 검수(audit)만
  python -m agent.review_eval --old      # 10-24 검수(review_two_stage, 원글 제목·본문만)와 나란히
"""
from __future__ import annotations

import argparse
import json
import sys

from . import claude, intent, judge

CASES = "data/review_eval.json"
OUT = "data/review_eval_result.json"


def run(path: str = CASES, old: bool = False, only: list[str] | None = None) -> dict:
    cases = json.load(open(path, encoding="utf-8"))
    if only:
        cases = [c for c in cases if c["id"] in only]
    rows, cost = [], {"new": 0.0, "old": 0.0}
    for c in cases:
        p = c["post"]
        new, usd = judge.audit(p, c.get("comments", []), c["draft"], c.get("facts", []), c.get("complexes", []),
                               c["link"], c.get("channel", "naver_cafe"), intent.style(c.get("channel", "naver_cafe")))
        cost["new"] += usd
        row = {"id": c["id"], "label": c["label"], "expect": c.get("expect", []), "note": c.get("note", ""),
               "new_pass": new["pass"], "new_failed": new.get("failed", []), "new_by": new.get("by"),
               "new_issues": new.get("issues", [])[:4]}
        if old:
            o, usd2 = judge.review_two_stage(c["draft"], c.get("facts", []), c.get("channel", "naver_cafe"),
                                             question=f"{p.get('title', '')}\n{p.get('text', '')}")
            cost["old"] += usd2
            row.update(old_pass=bool(o.get("pass")), old_by=o.get("by"), old_issues=o.get("issues", [])[:3])
        rows.append(row)
        ok_new = row["new_pass"] == (c["label"] == "good")
        print(f"{'✓' if ok_new else '✗'} {c['id']:<16} {c['label']:<4} new={'pass' if row['new_pass'] else 'FAIL'}"
              f" {','.join(row['new_failed'])[:60]:<40}" + (f" old={'pass' if row.get('old_pass') else 'FAIL'}" if old else ""),
              flush=True)

    def tally(key: str) -> dict:
        bad = [r for r in rows if r["label"] == "bad"]
        good = [r for r in rows if r["label"] == "good"]
        return {"bad_total": len(bad), "bad_caught": sum(not r[key] for r in bad),
                "good_total": len(good), "good_passed": sum(r[key] for r in good),
                "bad_missed": [r["id"] for r in bad if r[key]], "good_blocked": [r["id"] for r in good if not r[key]]}
    summary = {"new": tally("new_pass"), "cost_usd": {k: round(v, 4) for k, v in cost.items()}}
    if old:
        summary["old"] = tally("old_pass")
    # 떨어뜨린 사례가 '맞는 이유'로 떨어졌는가 — expect에 적은 검사가 실패 목록에 들어 있는지
    summary["new"]["right_reason"] = sum(1 for r in rows if r["label"] == "bad" and not r["new_pass"]
                                         and (set(r["expect"]) & set(r["new_failed"]) or not r["expect"]))
    json.dump({"summary": summary, "rows": rows, "spent": claude.spent()}, open(OUT, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--old", action="store_true")
    ap.add_argument("--cases", default=CASES)
    ap.add_argument("--only", nargs="*")
    a = ap.parse_args(argv)
    print(json.dumps(run(a.cases, a.old, a.only), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
