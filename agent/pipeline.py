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

from . import claude, config, discover, intent, judge, poster, queue, reader, reply
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


def _age(posted: str) -> int | None:
    """작성일('2026-10-22…')이 오늘로부터 며칠 전인가. 모르면 None."""
    if not posted:
        return None
    try:
        return (datetime.now(KST).date() - datetime.fromisoformat(posted[:10]).date()).days
    except ValueError:
        return None


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
    seen = {r["source_url"] for r in queue.load()}

    def _skip(base_: dict, reason: str, why: str = "", **kv) -> None:
        _append({**base_, **kv, "status": "skip", "removal_reason": reason, "why": why})
        summary["skip"] += 1

    # ── 1차: 값싼 거름(중복·질문·광고·작성일) + Claude 판정(Haiku, 검색 요약) ──
    keep: list[dict] = []
    for i, it in enumerate(found):
        if i % 5 == 0:
            _progress(running=True, total=len(found), processed=i, summary=summary, queries=queries,
                      current=it.get('title', '')[:60], provider=it.get('provider', ''))
        url = it.get("url", "")
        if not url or url in seen:
            summary["dup"] += 1
            continue
        seen.add(url)
        cur['id'] = hashlib.sha1(url.encode()).hexdigest()[:10]
        _emit('found', cur['id'], provider=it.get('provider', ''), channel=it.get('channel', ''), where=it.get('where', ''),
              title=it.get('title', '')[:80], query=it.get('query', ''), url=url, i=i + 1, total=len(found))
        channel = it.get("channel") or "naver_cafe"
        where = it.get("where", "")
        base = {"channel": channel, "source_url": url, "title": it.get("title", ""), "where": where,
                "query": it.get("query", ""), "provider": it.get("provider", ""), "intent": "compare"}
        text = f"{it.get('title', '')}\n{it.get('desc', '')}"
        # ★질문 글만 답한다. 다음 카페 실측(10-24): 중개 매물 광고 글이 단지 이름으로 걸려 사람 대기열을 채웠다.
        if not QUESTION.search(text) or AD.search(text):
            _skip(base, "not_question")
            continue
        # 오래된 질문은 답하지 않는다(이미 결론 난 글에 링크만 남는다). 지식iN은 날짜를 페이지에서 읽는다.
        posted = it.get("posted_at") or (discover.kin_date(url) if channel == "naver_kin" else "")
        age = _age(posted)
        if age is not None and age > STALE_DAYS:
            _skip(base, "stale", f"작성 {posted[:10]} — {age}일 지난 질문")
            continue
        _emit('question', cur['id'], ok=True)
        st = {"it": it, "id": cur['id'], "channel": channel, "where": where, "base": base, "text": text,
              "conf": 0.0, "ai_used": False, "ai_note": "", "ai_fields": {}, "spent": 0.0}
        if claude.available():
            try:
                tri, c1 = judge.triage(it.get("title", ""), it.get("desc", ""), channel, where, {})
                st["spent"] += c1
                tri_ok = bool(tri.get("is_question") and tri.get("appropriate"))   # 관련성은 추출·매칭 뒤에 판정(매칭 전 triage가 목동 글을 버렸다)
                st["conf"] = float(tri.get("confidence") or 0)
                _emit('ai_triage', cur['id'], ok=tri_ok, confidence=round(st["conf"], 2), reason=tri.get("reason", ""))
                st["ai_fields"] = {"ai_triage": tri.get("reason", "")[:200], "ai_conf": round(st["conf"], 2),
                                   "ai_cost": round(st["spent"], 6)}
                st["ai_used"] = True
                if not tri_ok:
                    _skip(base, "ai_reject", tri.get("reason", "")[:120], **st["ai_fields"])
                    summary["ai_reject"] = summary.get("ai_reject", 0) + 1
                    continue
            except claude.BudgetExceeded as e:
                st["ai_note"] = f"AI 건너뜀: {e}"
            except Exception as e:
                st["ai_note"] = f"AI 건너뜀: {type(e).__name__}: {str(e)[:60]}"
        keep.append(st)

    # ── 원문 읽기: 판정을 통과한 카페·블로그 글만. 카페는 Aside 탭 하나로 묶어 읽는다(게시판·본문·댓글·댓글 권한) ──
    _emit('read', None, total=len(keep))
    try:
        arts = reader.read_many([(s_["channel"], s_["it"]["url"]) for s_ in keep
                                 if s_["channel"] in ("naver_cafe", "naver_blog")]) if keep else {}
    except Exception as e:
        arts = {}
        _emit('read_error', None, error=f"{type(e).__name__}: {str(e)[:80]}")

    # ── 2차: 원문으로 거름(삭제·권한·작성일) → 추출(Sonnet) → 매칭·초안 → 최종 검수(코드 + Opus·Sonnet) ──
    for st in keep:
        it, base, channel, where = st["it"], st["base"], st["channel"], st["where"]
        url, ai_fields, conf, ai_used, ai_note, spent_ = it["url"], st["ai_fields"], st["conf"], st["ai_used"], st["ai_note"], st["spent"]
        cur['id'] = st["id"]
        rows = queue.load()
        art = arts.get(url) or {}
        if art.get("deleted"):
            _skip(base, "deleted", "삭제되었거나 없는 글", **ai_fields)
            continue
        if channel == "naver_cafe" and "레벨" in str(art.get("reason", "")):
            _skip(base, "no_permission", "카페 등급 부족 — " + str(art.get("reason", ""))[:60], **ai_fields)
            summary["no_permission"] = summary.get("no_permission", 0) + 1
            continue
        if channel == "naver_cafe" and "멤버" in str(art.get("reason", "")):
            # 미가입 카페 — 읽기·댓글 모두 막힌다. 어느 카페에 가입하면 후보가 늘지 세기 위해 따로 센다.
            _skip(base, "not_member", "미가입 카페(멤버 공개 글)", **ai_fields)
            nm = summary.setdefault("not_member", {})
            nm[where] = nm.get(where, 0) + 1
            continue
        if channel in ("naver_cafe", "naver_blog") and not art.get("ok"):
            # ★원문을 못 읽은 글은 검수할 수 없다 — 요약만 보고 통과시키지 않는다
            _skip(base, "unread", "원문 읽기 실패: " + str(art.get("reason", ""))[:80], **ai_fields)
            continue
        if channel == "naver_cafe" and not art.get("can_comment"):
            _skip(base, "no_permission", "댓글 권한 없음 — 미가입·등급 제한 " + str(art.get("no_comment_why", ""))[:60], **ai_fields)
            summary["no_permission"] = summary.get("no_permission", 0) + 1
            continue
        age = _age(art.get("written", ""))
        if age is not None and age > STALE_DAYS:
            _skip(base, "stale", f"작성 {art['written'][:10]} — {age}일 지난 질문", **ai_fields)
            continue
        # 같은 카페는 한 번 실행에 한 글만 넘긴다(도배 방지 — 게시 상한과 별개).
        # 지식iN은 where가 사이트 전체('지식iN')라 이 규칙을 걸면 하루 한 질문만 남는다 — 하루 게시 상한으로만 막는다.
        if where and channel != "naver_kin" and any(r.get("where") == where and r.get("status") in ("ready", "needs_human")
                         and r.get("found_at", "")[:10] == datetime.now(KST).strftime("%Y-%m-%d") for r in rows):
            _skip(base, "same_cafe_today", **ai_fields)
            continue
        title = art.get("subject") or it.get("title", "")
        body = art.get("text") or it.get("desc", "")
        text = f"{title}\n{body}"
        post = {"board": art.get("board", ""), "title": title, "text": body, "written": art.get("written", "")}
        placement = f"{channel}-{datetime.now(KST):%m%d}-{len(rows) + 1}"
        base["placement_id"] = placement
        ents = None
        if ai_used:
            try:
                ents, c2 = judge.extract(title, body)
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
            _skip(base, "error", f"{type(e).__name__}: {str(e)[:80]}", **ai_fields)
            continue
        if not res.get("ok"):
            _skip(base, "no_match", res.get("why", ""), **ai_fields)
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
        ai_ok = False
        # ── Claude 3·4단계: draft(Sonnet, facts 숫자만) → audit(코드 + Opus·Sonnet 독립 판정, 원문·댓글·단지 주소). 규칙 초안은 폴백 ──
        if ai_used and not low:
            if conf < 0.6:
                r_ = _append({**row, "status": "needs_human", "why": "AI 확신 낮음 — " + row.get("ai_triage", "")[:80]})
                summary["needs_human"] += 1
                new_human.append(r_)
                continue
            try:
                cands = []
                try:
                    dr, c3 = judge.draft({"title": title, "text": body}, ents or {},
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
                    rev, c4 = judge.audit(post, art.get("comments") or [], txt, res.get("facts", []),
                                          res.get("complexes", []), res["links"][0], channel, intent.style(channel))
                    spent_ += c4
                    _emit('ai_review', cur['id'], ok=bool(rev.get("pass")), issues=rev.get("issues", [])[:4],
                          by=rev.get("by", ""), draft_by=src, failed=rev.get("failed", []))
                    last = rev
                    if rev.get("pass"):
                        passed = (src, txt)
                        break
                    if rev.get("by") != "code" and set(rev.get("failed", [])) - {"facts_only", "honest_tone"}:
                        break                  # 글·단지·게시판 문제는 초안을 바꿔도 같다 — 다음 초안은 심사하지 않는다
                row["ai_cost"] = round(spent_, 6)
                if not passed:
                    row["ai_review"] = "; ".join((last or {}).get("issues", []))[:300]
                    hard = set((last or {}).get("failed", [])) & {"live_question", "right_target", "not_redundant", "fits_board"}
                    if hard:                   # 사람이 봐도 올리지 않을 글 — 사람 대기열에 넣지 않는다
                        _append({**row, "status": "skip", "removal_reason": "audit_" + sorted(hard)[0],
                                 "why": "검수 탈락 — " + row["ai_review"][:120]})
                        summary["skip"] += 1
                        summary["audit_reject"] = summary.get("audit_reject", 0) + 1
                        continue
                    r_ = _append({**row, "status": "needs_human", "why": "검수 걸림 — " + row["ai_review"][:120]})
                    summary["needs_human"] += 1
                    new_human.append(r_)
                    continue
                final_text = passed[1]
                row.update(draft=final_text, draft_hash=hashlib.sha1(final_text.encode()).hexdigest()[:12],
                           ai_review=f"통과({passed[0]} 초안 · {last.get('by', '')} 검수 · 확신 {last.get('confidence', 0):.2f})")
                ai_ok = last.get("confidence", 0) >= 0.7     # 두 모델 중 낮은 쪽 확신
            except claude.BudgetExceeded as e:
                ai_used, ai_note = False, f"AI 건너뜀: {e}"
            except Exception as e:
                ai_used, ai_note = False, f"AI 건너뜀: {type(e).__name__}: {str(e)[:60]}"
        note = f" · {ai_note}" if ai_note else ""
        posted_today = _today_posts(rows)
        cap_ok = len(posted_today) < daily_cap and not any(r.get("where") == where for r in posted_today)
        if allowed and publish and cap_ok and ai_ok:
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
        elif low:
            # ★내용 점수(적합도+데이터+최신성, 만점 8)가 6 미만이면 사람에게도 넘기지 않는다.
            #   다음 카페 실측(10-24): 무관한 글이 허브 이름 하나로 걸려 사람 대기열 42건이 쌓였다.
            _append({**row, "status": "skip", "removal_reason": "low_score", "why": f"내용 점수 {s - rs} < 6"})
            summary["skip"] += 1
        elif ai_ok:
            # 원문·댓글을 본 최종 검수(Opus·Sonnet)까지 통과 — 문장은 고칠 필요 없음. 게시는 운영자 확인 뒤.
            rule_txt = "규칙 미확인 채널" if rs < 2 else ("오늘 상한 도달" if not cap_ok else "자동 게시 꺼짐")
            _append({**row, "status": "ready", "why": f"검수 통과(확신 {last.get('confidence', 0):.2f}) · {rule_txt}"})
            summary["ready"] += 1
        else:
            why = ("검수 확신 0.7 미만 — 사람 확인" if ai_used else
                   "AI 없이 만든 초안 — 사람 확인")
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
