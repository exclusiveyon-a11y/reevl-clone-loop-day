"""전 과정 — 찾기 → 매칭 → 초안 → 점수 → 큐 → (허용 채널이면) Aside 게시 → 실패하면 사람 대기열.

자동 게시 조건(전부 만족해야 한다):
- publish=True (기본 False — 기본 실행은 초안·큐까지만)
- 점수 8점 이상이고 규칙 점수 2점(data/rules.json에 그 채널·그 카페가 allowed=true로 확인돼 있음)
- 오늘(KST) 게시 수 < daily_cap(기본 3), 같은 카페(where)는 하루 1건
- 같은 초안 문구를 다시 쓰지 않는다(초안 해시)
rules.json이 없으면 규칙 점수가 전부 0이라 자동 게시는 0건이고, 모든 후보가 사람 대기열로 간다.
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime

import re

from . import claude, config, discover, intent, judge, poster, queue, reply
from .queue import KST

# 질문 신호 — 묻는 글에만 답한다. 광고 신호 — 중개 매물·분양 홍보 글은 건너뛴다.
QUESTION = re.compile(r"\?|？|고민|어디|어떤가요|어떨까요|괜찮|추천|vs|VS|비교|질문|문의|궁금|조언|선택|어느")
AD = re.compile(r"급매|매물|분양\s*문의|상담\s*문의|공인중개|중개사무소|입주자\s*모집|모델하우스|☎|010-\d")

STALE_DAYS = int(os.environ.get("GTM_STALE_DAYS", "14"))

SAMPLE_RULES = {"naver_cafe": {"<카페명>": {"allowed": True, "rule_url": "<게시판 공지 URL>", "checked_at": "YYYY-MM-DD"}}}


def load_rules(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return {}


def rule_score(rules: dict, channel: str, where: str) -> tuple[int, str]:
    r = (rules.get(channel) or {}).get(where or "")
    if r and r.get("allowed") is True:
        return 2, r.get("rule_url", "")
    return 0, ""


def fresh_score(rank) -> int:
    try:
        rank = int(rank)
    except (TypeError, ValueError):
        return 0
    return 2 if rank < 5 else 1 if rank < 15 else 0


def notify(row: dict) -> None:
    """사람 대기열 알림 — 텔레그램 키가 있으면 보내고, 없으면 콘솔. 토큰은 출력하지 않는다."""
    msg = row.get("_text") or (f"[리블 GTM 사람 게시 필요] {row.get('channel')} · {row.get('where')}\n{row.get('source_url')}\n"
                               f"사유: {row.get('why') or row.get('status')}\n\n{row.get('draft')}")
    tok, chat = config.get("TELEGRAM_BOT_TOKEN"), config.get("TELEGRAM_CHAT_ID")
    if tok and chat:
        try:
            data = urllib.parse.urlencode({"chat_id": chat, "text": msg[:3900], "disable_web_page_preview": "true"}).encode()
            urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data=data, timeout=15)
            return
        except Exception as e:
            print(f"  (텔레그램 알림 실패: {type(e).__name__}) — 콘솔로 대신 출력")
    print("\n" + msg + "\n")


def _search(queries: list[str], providers: list[str] | None) -> list[dict]:
    from . import discover
    if hasattr(discover, "search_all"):
        return discover.search_all(queries, providers)
    out = []
    for q in queries:                                 # 폴백: 네이버 카페만
        for it in discover.search(q, "naver_cafe"):
            it.setdefault("provider", "naver")
            out.append(it)
    return out


def _today_posts(rows: list[dict]) -> list[dict]:
    today = datetime.now(KST).strftime("%Y-%m-%d")
    return [r for r in rows if r.get("status") == "posted" and (r.get("posted_at") or "").startswith(today)]


def run(queries: list[str], providers: list[str] | None = None, publish: bool = False, daily_cap: int = 3,
        rules_path: str = "data/rules.json", qa: bool = False, notify_human: bool = True,
        notify_summary: bool = False) -> dict:
    new_human: list[dict] = []
    if publish and os.environ.get("GTM_AUTOPUBLISH") != "1":
        print("  (자동 게시 중지 상태 — GTM_AUTOPUBLISH=1일 때만 게시. 이번 실행은 초안까지)")
        publish = False
    rules = load_rules(rules_path)
    _events_reset()
    _emit('run_start', None, queries=queries, providers=providers or [])
    found = _search(queries, providers)
    _emit('searched', None, total=len(found))
    cur = {'id': ''}

    def _append(row: dict) -> dict:
        r = queue.append(row)
        _emit('result', cur['id'], status=row.get('status'), reason=row.get('removal_reason', ''),
              why=row.get('why', ''), score=row.get('score', ''), draft=row.get('draft', ''), link=row.get('link', ''))
        return r
    summary = {"found": len(found), "matched": 0, "ready": 0, "posted": 0, "needs_human": 0, "skip": 0, "dup": 0}
    _progress(running=True, total=len(found), processed=0, summary=summary, queries=queries)
    for i, it in enumerate(found):
        if i % 5 == 0:
            _progress(running=True, total=len(found), processed=i, summary=summary, queries=queries,
                      current=it.get('title', '')[:60], provider=it.get('provider', ''))
        rows = queue.load()
        url = it.get("url", "")
        if not url or any(r["source_url"] == url for r in rows):
            summary["dup"] += 1
            continue
        cur['id'] = hashlib.sha1(url.encode()).hexdigest()[:10]
        _emit('found', cur['id'], provider=it.get('provider', ''), channel=it.get('channel', ''), where=it.get('where', ''),
              title=it.get('title', '')[:80], query=it.get('query', ''), url=url, i=i + 1, total=len(found))
        channel = it.get("channel") or "naver_cafe"
        where = it.get("where", "")
        placement = f"{channel}-{datetime.now(KST):%m%d}-{len(rows) + 1}"
        base = {"channel": channel, "source_url": url, "title": it.get("title", ""), "where": where,
                "query": it.get("query", ""), "provider": it.get("provider", ""), "placement_id": placement,
                "intent": "compare"}
        text = f"{it.get('title', '')}\n{it.get('desc', '')}"
        # ★질문 글만 답한다. 다음 카페 실측(10-24): 중개 매물 광고 글이 단지 이름으로 걸려 사람 대기열을 채웠다.
        if not QUESTION.search(text) or AD.search(text):
            _append({**base, "status": "skip", "removal_reason": "not_question"})
            summary["skip"] += 1
            continue
        # 오래된 질문은 답하지 않는다(이미 결론 난 글에 링크만 남는다). 지식iN은 날짜를 페이지에서 읽는다.
        posted = it.get("posted_at") or (discover.kin_date(url) if channel == "naver_kin" else "")
        if posted:
            try:
                age = (datetime.now(KST).date() - datetime.fromisoformat(posted[:10]).date()).days
            except ValueError:
                age = 0
            if age > STALE_DAYS:
                _append({**base, "status": "skip", "removal_reason": "stale", "why": f"작성 {posted[:10]} — {age}일 지난 질문"})
                summary["skip"] += 1
                continue
        _emit('question', cur['id'], ok=True)
        # 같은 카페는 한 번 실행에 한 글만 사람에게 넘긴다(도배 방지 — 게시 상한과 별개).
        # 지식iN은 where가 사이트 전체('지식iN')라 이 규칙을 걸면 하루 한 질문만 남는다 — 하루 게시 상한으로만 막는다.
        if where and channel != "naver_kin" and any(r.get("where") == where and r.get("status") in ("ready", "needs_human")
                         and r.get("found_at", "")[:10] == datetime.now(KST).strftime("%Y-%m-%d") for r in rows):
            _append({**base, "status": "skip", "removal_reason": "same_cafe_today"})
            summary["skip"] += 1
            continue
        # ── Claude 1·2단계(키·예산이 있을 때만): triage(Haiku) → extract(Sonnet). 실패하면 규칙 흐름 ──
        ai_used, ai_ok, ai_note, conf, ents, spent_ = False, False, "", 0.0, None, 0.0
        ai_fields: dict = {}
        if claude.available():
            try:
                tri, c1 = judge.triage(it.get("title", ""), it.get("desc", ""), channel, where, {})
                spent_ += c1
                tri_ok = bool(tri.get("is_question") and tri.get("appropriate"))   # 관련성은 추출·매칭 뒤에 판정(매칭 전 triage가 목동 글을 버렸다)
                conf = float(tri.get("confidence") or 0)
                _emit('ai_triage', cur['id'], ok=tri_ok, confidence=round(conf, 2), reason=tri.get("reason", ""))
                ai_fields = {"ai_triage": tri.get("reason", "")[:200], "ai_conf": round(conf, 2), "ai_cost": round(spent_, 6)}
                ai_used = True
                if not tri_ok:
                    _append({**base, **ai_fields, "status": "skip", "removal_reason": "ai_reject", "why": tri.get("reason", "")[:120]})
                    summary["skip"] += 1
                    summary["ai_reject"] = summary.get("ai_reject", 0) + 1
                    continue
                ents, c2 = judge.extract(it.get("title", ""), it.get("desc", ""))
                spent_ += c2
                ai_fields["ai_cost"] = round(spent_, 6)
                _emit('ai_extract', cur['id'], complexes=(ents.get("complexes") or [])[:3], gu=ents.get("gu", ""),
                      dong=ents.get("dong", ""), concern=ents.get("concern", ""), purpose=ents.get("purpose", ""))
            except claude.BudgetExceeded as e:
                ai_used, ai_note = False, f"AI 건너뜀: {e}"
            except Exception as e:
                ai_used, ai_note = False, f"AI 건너뜀: {type(e).__name__}: {str(e)[:60]}"
        try:
            res = reply.build(text, channel, placement, qa, entities=ents)
        except Exception as e:                 # 한 건의 API 지연이 실행 전체를 끊지 않게
            _append({**base, **ai_fields, "status": "skip", "removal_reason": "error", "why": f"{type(e).__name__}: {str(e)[:80]}"})
            summary["skip"] += 1
            continue
        if not res.get("ok"):
            _append({**base, **ai_fields, "status": "skip", "removal_reason": "no_match", "why": res.get("why", "")})
            summary["skip"] += 1
            continue
        summary["matched"] += 1
        _emit('match', cur['id'], ok=True, kind=res.get('kind', ''), intents=res.get('intents', []), via=res.get('match_via', ''),
              complexes=[c.get('name') for c in res.get('complexes', [])][:3], hubs=[h.get('name') for h in res.get('hubs', [])][:2])
        rs, rule_url = rule_score(rules, channel, where)
        fr = fresh_score(it.get("rank"))
        s, allowed = queue.score(res["fit"], res["data"], fr, rs)
        h = hashlib.sha1(res["text"].encode()).hexdigest()[:12]
        _emit('score', cur['id'], fit=res['fit'], data=res['data'], fresh=fr, rule=rs, total=s)
        row = {**base, **ai_fields, "score": s, "allowed": allowed, "rule_url": rule_url,
               "fit": res["fit"], "data": res["data"], "fresh": fr, "rule": rs,
               "rule_checked_at": (rules.get(channel) or {}).get(where, {}).get("checked_at", "") if rs else "",
               "draft": res["text"], "link": res["links"][0], "draft_hash": h}
        if any(r.get("draft_hash") == h for r in rows):
            _append({**row, "status": "skip", "removal_reason": "same_draft"})
            summary["skip"] += 1
            continue
        low = (not allowed) and (s - rs < 6)     # 내용 점수 미달은 AI를 더 부르지 않는다(비용 절약)
        final_text = res["text"]
        # ── Claude 3·4단계: draft(Sonnet, facts 숫자만) → review(Haiku, 애매하면 Opus 재심). 규칙 초안은 폴백 ──
        if ai_used and not low:
            if conf < 0.6:
                r_ = _append({**row, "status": "needs_human", "why": "AI 확신 낮음 — " + row.get("ai_triage", "")[:80]})
                summary["needs_human"] += 1
                new_human.append(r_)
                continue
            try:
                cands = []
                try:
                    dr, c3 = judge.draft({"title": it.get("title", ""), "text": it.get("desc", "")}, ents or {},
                                         res.get("facts", []), channel, res["links"][0], intent.style(channel))
                    spent_ += c3
                    _emit('ai_draft', cur['id'], ok=bool(dr.get("text")), chars=len(dr.get("text", "")))
                    if dr.get("text"):
                        cands.append(("sonnet", dr["text"]))
                except claude.BudgetExceeded:
                    raise
                except Exception as e:
                    _emit('ai_draft', cur['id'], ok=False, chars=0, error=type(e).__name__)
                cands.append(("rule", res["text"]))
                passed, last = None, None
                for src, txt in cands:
                    rev, c4 = judge.review_two_stage(txt, res.get("facts", []), channel, question=text)
                    spent_ += c4
                    _emit('ai_review', cur['id'], ok=bool(rev.get("pass")), issues=rev.get("issues", [])[:4],
                          by=rev.get("by", ""), draft_by=src)
                    last = rev
                    if rev.get("pass"):
                        passed = (src, txt)
                        break
                row["ai_cost"] = round(spent_, 6)
                if not passed:
                    row["ai_review"] = "; ".join((last or {}).get("issues", []))[:200]
                    r_ = _append({**row, "status": "needs_human", "why": "심사 걸림 — " + row["ai_review"][:100]})
                    summary["needs_human"] += 1
                    new_human.append(r_)
                    continue
                final_text = passed[1]
                row.update(draft=final_text, draft_hash=hashlib.sha1(final_text.encode()).hexdigest()[:12],
                           ai_review=f"통과({passed[0]} 초안 · {last.get('by', '')} 심사)")
                ai_ok = conf >= 0.75
            except claude.BudgetExceeded as e:
                ai_used, ai_note = False, f"AI 건너뜀: {e}"
            except Exception as e:
                ai_used, ai_note = False, f"AI 건너뜀: {type(e).__name__}: {str(e)[:60]}"
        note = f" · {ai_note}" if ai_note else ""
        posted_today = _today_posts(rows)
        cap_ok = len(posted_today) < daily_cap and not any(r.get("where") == where for r in posted_today)
        if allowed and publish and cap_ok and (ai_ok or not ai_used):
            out = poster.post(channel, url, final_text, dry=False)
            if out.get("ok") and out.get("mode") == "posted":
                _append({**row, "status": "posted", "posted_url": out.get("url", url),
                              "posted_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M")})
                summary["posted"] += 1
            else:
                r_ = _append({**row, "status": "needs_human", "why": out.get("why", "게시 실패") + note})
                summary["needs_human"] += 1
                if notify_human:
                    notify(r_)
                new_human.append(r_)
        elif allowed and not publish and (ai_ok or not ai_used):
            _append({**row, "status": "ready", "why": ("AI 승인 · " if ai_ok else "") + "publish 꺼짐 — 초안까지" + note})
            summary["ready"] += 1
        elif low:
            # ★내용 점수(적합도+데이터+최신성, 만점 8)가 6 미만이면 사람에게도 넘기지 않는다.
            #   다음 카페 실측(10-24): 무관한 글이 허브 이름 하나로 걸려 사람 대기열 42건이 쌓였다.
            _append({**row, "status": "skip", "removal_reason": "low_score", "why": f"내용 점수 {s - rs} < 6"})
            summary["skip"] += 1
        elif ai_ok:
            # AI 판정·추출·작성·심사 통과 — 사람 확인 불필요. 게시는 규칙 허용 채널의 자동 게시 또는 사람이 복사·게시.
            rule_txt = "규칙 미확인 채널 — 게시는 사람이" if rs < 2 else ("오늘 상한 도달" if not cap_ok else "자동 게시 꺼짐")
            _append({**row, "status": "ready", "why": f"AI 승인(확신 {row.get('ai_conf')}) · {rule_txt}"})
            summary["ready"] += 1
        else:
            why = ("AI 확신 0.6~0.75 — 사람 확인" if ai_used else
                   "규칙 미확인 채널 — 사람이 규칙 확인 후 게시" if rs < 2 else
                   "오늘 상한 도달" if not cap_ok else f"점수 {s} < 8")
            r_ = _append({**row, "status": "needs_human", "why": why + note})
            summary["needs_human"] += 1
            if notify_human and publish:
                notify(r_)
            new_human.append(r_)
    summary["human_new"] = len(new_human)
    den = summary["ready"] + summary["needs_human"]
    summary["human_rate"] = round(summary["needs_human"] / den, 3) if den else 0.0
    _progress(running=False, total=len(found), processed=len(found), summary=summary, queries=queries)
    _emit('run_end', None, summary=summary)
    if notify_summary and new_human:
        digest(new_human, summary)
    return summary


EVENTS = os.environ.get("GTM_EVENTS", "data/events.jsonl")
_SEQ = {"n": 0}


def _events_reset() -> None:
    """실행마다 이벤트 파일을 새로 시작한다 — 관리자 화면이 seq로 이어 읽는다."""
    _SEQ["n"] = 0
    try:
        os.makedirs(os.path.dirname(EVENTS) or ".", exist_ok=True)
        open(EVENTS, "w", encoding="utf-8").close()
    except Exception:
        pass


def _emit(stage: str, item_id: str | None, **kv) -> None:
    """라이브 이벤트 한 줄(JSONL). 질문 원문·닉네임은 담지 않는다(제목·채널·카페명·판단 결과만)."""
    _SEQ["n"] += 1
    ev = {"seq": _SEQ["n"], "t": datetime.now(KST).strftime("%H:%M:%S"), "stage": stage, "id": item_id or "", **kv}
    try:
        with open(EVENTS, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except Exception:
        pass


PROGRESS = os.environ.get("GTM_PROGRESS", "data/run_status.json")


def _progress(**kv) -> None:
    """실행 진행 상황 — 관리자 화면이 몇 초마다 읽어 막대로 보여준다."""
    kv["updated_at"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    try:
        os.makedirs(os.path.dirname(PROGRESS) or ".", exist_ok=True)
        json.dump(kv, open(PROGRESS, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass


def digest(rows: list[dict], summary: dict, top: int = 3) -> None:
    """실행 요약 알림 — 사람 확인 후보 상위 몇 건을 붙여 넣을 초안과 함께 한 통으로 보낸다."""
    best = sorted(rows, key=lambda r: -int(r.get("score") or 0))[:top]
    head = (f"[리블 GTM] 발견 {summary['found']} · 매칭 {summary['matched']} · 사람 확인 {len(rows)}건"
            f" (상위 {len(best)}건)")
    parts = [head]
    for r in best:
        parts.append(f"\n— {r.get('where')} · 점수 {r.get('score')}\n{r.get('title', '')[:60]}\n글: {r.get('source_url')}\n\n{r.get('draft')}")
    notify({"channel": "", "where": "", "source_url": "", "why": "", "draft": "", "_text": "\n".join(parts)})
