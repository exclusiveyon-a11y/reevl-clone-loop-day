"""GTM Agent 관리자 — 발견부터 게시까지 전 과정을 한 화면에서 보고 조작한다. **로컬 전용**.

실행:
  python -m agent.admin                 # data/queue.csv (GTM_QUEUE로 바꿀 수 있다)
  python -m agent.admin --demo          # examples/queue.sample.csv 읽기 전용
  python -m agent.admin --port 8800

- 127.0.0.1에만 바인드한다. 외부 의존성 없음(표준 라이브러리).
- 시작할 때 무작위 토큰을 만들어 페이지에 심는다. POST는 X-Admin-Token이 맞을 때만 받는다(CSRF 방지).
- 게시 자체는 하지 않는다. 사람이 게시한 뒤 '게시 완료 표시'로 기록하거나, 제외·사람 확인으로 상태를 바꾼다.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import queue

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "examples" / "queue.sample.csv"
RULES = Path(os.environ.get("GTM_RULES", "data/rules.json"))
DAILY_CAP = int(os.environ.get("GTM_DAILY_CAP", "3"))
STATUSES = {"posted", "ready", "needs_human", "skip"}
TOKEN = secrets.token_urlsafe(24)
DEMO = False


def rows() -> list[dict]:
    if DEMO:
        with open(SAMPLE, newline="", encoding="utf-8") as f:
            return [{k: (r.get(k) or "") for k in set(queue.FIELDS) | set(r)} for r in csv.DictReader(f)]
    return queue.load()


def rules_info() -> dict:
    if not RULES.exists():
        return {"cafes": 0, "note": "허용 목록 없음 → 자동 게시 꺼짐"}
    try:
        d = json.loads(RULES.read_text(encoding="utf-8"))
    except Exception:
        return {"cafes": 0, "note": "rules.json을 읽지 못함 → 자동 게시 꺼짐"}
    n = sum(1 for ch in d.values() if isinstance(ch, dict) for v in ch.values() if isinstance(v, dict) and v.get("allowed"))
    return {"cafes": n, "note": f"허용 카페 {n}곳" if n else "허용된 카페 0곳 → 자동 게시 꺼짐"}


def payload() -> dict:
    return {"rows": rows(), "demo": DEMO, "cap": DAILY_CAP, "rules": rules_info(),
            "today": datetime.now(KST).strftime("%Y-%m-%d"), "at": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")}


class H(BaseHTTPRequestHandler):
    server_version = "gtm-admin"

    def log_message(self, fmt, *args):  # 조용히
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                         "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode())

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            html = PAGE.replace("__TOKEN__", TOKEN).replace("__DEMO__", "true" if DEMO else "false")
            return self._send(200, html.encode(), "text/html; charset=utf-8")
        if self.path == "/api/queue":
            return self._json(200, payload())
        self._json(404, {"ok": False, "why": "없는 경로"})

    def do_POST(self):
        if self.headers.get("X-Admin-Token") != TOKEN:
            return self._json(403, {"ok": False, "why": "토큰이 맞지 않습니다"})
        if self.path != "/api/mark":
            return self._json(404, {"ok": False, "why": "없는 경로"})
        if DEMO:
            return self._json(409, {"ok": False, "why": "데모(읽기 전용)입니다"})
        try:
            n = min(int(self.headers.get("Content-Length") or 0), 20000)
            d = json.loads(self.rfile.read(n) or b"{}")
            cid, st = str(d.get("candidate_id", "")), str(d.get("status", ""))
            if st not in STATUSES:
                return self._json(400, {"ok": False, "why": "알 수 없는 상태"})
            kv = {"status": st}
            if st == "posted":
                url = str(d.get("posted_url", "")).strip()
                if not url.startswith("https://"):
                    return self._json(400, {"ok": False, "why": "게시 URL(https://…)이 필요합니다"})
                kv.update(posted_url=url[:500], posted_at=datetime.now(KST).strftime("%Y-%m-%d %H:%M"))
            if st == "skip":
                kv["removal_reason"] = str(d.get("removal_reason", "")).strip()[:200]
            row = queue.mark(cid, **kv)
            return self._json(200, {"ok": True, "row": row})
        except KeyError:
            return self._json(404, {"ok": False, "why": "그 후보가 큐에 없습니다"})
        except Exception as e:  # noqa: BLE001
            return self._json(500, {"ok": False, "why": str(e)[:200]})


PAGE = r"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>리블 GTM Agent 관리자</title>
<style>
:root{--bg:#f7f8fa;--card:#fff;--ink:#191f28;--sub:#616c7a;--dim:#8b95a1;--line:#e5e8eb;--chip:#f2f4f6;
--brand:#3182f6;--brand-soft:#eaf3ff;--pos:#12b886;--pos-soft:#e6f7f1;--warn:#e8a33d;--warn-soft:#fbf1e0;--neg:#f04452;--neg-soft:#fdecee;
--f:"Pretendard Variable",Pretendard,"Apple SD Gothic Neo",-apple-system,BlinkMacSystemFont,"Malgun Gothic",sans-serif;
--mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#0e0f11;--card:#16171a;--ink:#edeef1;--sub:#aab1bc;--dim:#79818d;--line:#282a2d;--chip:#232529;
--brand:#4d9fff;--brand-soft:#16233a;--pos:#2dd4a0;--pos-soft:#122a24;--warn:#f0b254;--warn-soft:#2e2413;--neg:#ff6b76;--neg-soft:#2f1619;color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--f);font-size:14px;line-height:1.55;word-break:keep-all;overflow-wrap:anywhere}
.wrap{max-width:1180px;margin:0 auto;padding:24px max(16px,env(safe-area-inset-left)) 48px max(16px,env(safe-area-inset-right))}
header{display:flex;flex-wrap:wrap;align-items:baseline;gap:8px 16px;margin-bottom:18px}
h1{font-size:22px;margin:0;letter-spacing:-.01em}.sub{color:var(--sub);font-size:13px}
.badge{font-size:12px;font-weight:600;border-radius:999px;padding:2px 10px;background:var(--warn-soft);color:var(--warn)}
.tiles{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:10px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px}
.tile .k{color:var(--sub);font-size:12px}.tile .v{font-size:24px;font-weight:700;font-variant-numeric:tabular-nums;margin-top:2px}
.tile .v small{font-size:13px;color:var(--dim);font-weight:500}
@media(max-width:900px){.tiles{grid-template-columns:repeat(3,minmax(0,1fr))}}@media(max-width:520px){.tiles{grid-template-columns:repeat(2,minmax(0,1fr))}}
.flow{display:flex;gap:6px;margin:16px 0 20px;overflow-x:auto;padding-bottom:4px}
.step{flex:1 0 120px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 12px;position:relative}
.step .n{font-size:20px;font-weight:700;font-variant-numeric:tabular-nums}.step .l{font-size:12px;color:var(--sub)}
.step .bar{height:4px;border-radius:2px;background:var(--brand);margin-top:8px}
.step.end{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;flex:2 0 240px}
.step.end div{text-align:center}.step.end .n{font-size:18px}
.controls{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px}
select,input[type=search]{font:inherit;color:var(--ink);background:var(--card);border:1px solid var(--line);border-radius:10px;padding:7px 10px;min-width:0}
input[type=search]{flex:1 1 220px}
.list{display:grid;gap:10px}
.item{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--dim);border-radius:12px;padding:14px 16px;display:grid;gap:8px;min-width:0}
.item.posted{border-left-color:var(--pos)}.item.ready{border-left-color:var(--brand)}.item.needs_human{border-left-color:var(--warn)}.item.skip{border-left-color:var(--dim)}.item.removed{border-left-color:var(--neg)}
.row{display:flex;flex-wrap:wrap;gap:6px 10px;align-items:center}
.chip{font-size:12px;font-weight:600;border-radius:999px;padding:2px 9px;background:var(--chip);color:var(--sub)}
.st-posted{background:var(--pos-soft);color:var(--pos)}.st-ready{background:var(--brand-soft);color:var(--brand)}.st-needs_human{background:var(--warn-soft);color:var(--warn)}.st-removed{background:var(--neg-soft);color:var(--neg)}
.title{font-weight:600;font-size:15px}.meta{color:var(--sub);font-size:12px}
.score{font-family:var(--mono);font-size:12px;color:var(--sub)}.score b{color:var(--ink)}
pre.draft{margin:0;white-space:pre-wrap;background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:10px 12px;font-family:var(--f);font-size:13px;max-height:220px;overflow:auto}
.link{font-family:var(--mono);font-size:12px;color:var(--brand);word-break:break-all}
.why{font-size:12px;color:var(--warn)}.reason{font-size:12px;color:var(--neg)}
button,.btn{font:inherit;font-size:13px;font-weight:600;border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:9px;padding:6px 11px;cursor:pointer;text-decoration:none}
button.primary{background:var(--brand);border-color:var(--brand);color:#fff}button:disabled{opacity:.45;cursor:not-allowed}
button:focus-visible,.btn:focus-visible,select:focus-visible,input:focus-visible{outline:2px solid var(--brand);outline-offset:1px}
footer{margin-top:22px;color:var(--sub);font-size:12px;display:flex;flex-wrap:wrap;gap:6px 18px}
.empty{color:var(--sub);background:var(--card);border:1px dashed var(--line);border-radius:12px;padding:28px;text-align:center}
.toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:var(--ink);color:var(--bg);padding:8px 14px;border-radius:10px;font-size:13px;opacity:0;transition:opacity .2s}
.toast.on{opacity:1}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style></head><body><div class="wrap">
<header><h1>리블 GTM Agent</h1><span class="sub">발견 → 매칭 → 초안 → 점수 → 게시·사람 확인 · 로컬 전용</span><span id="demo" class="badge" hidden>데모 · 읽기 전용</span></header>
<section class="tiles" id="tiles"></section>
<section class="flow" id="flow" aria-label="파이프라인 단계별 건수"></section>
<section class="controls">
  <select id="fStatus" aria-label="상태"><option value="">모든 상태</option><option value="ready">게시 대기</option><option value="needs_human">사람 확인</option><option value="posted">게시됨</option><option value="skip">제외</option></select>
  <select id="fProv" aria-label="채널"><option value="">모든 채널</option></select>
  <select id="fWhere" aria-label="카페"><option value="">모든 카페·게시판</option></select>
  <input id="fQ" type="search" placeholder="제목·검색어·초안 검색" aria-label="검색">
</section>
<section class="list" id="list"></section>
<footer id="foot"></footer></div><div class="toast" id="toast" role="status"></div>
<script>
const TOKEN="__TOKEN__", DEMO=__DEMO__;
const ST={posted:"게시됨",ready:"게시 대기",needs_human:"사람 확인",skip:"제외"};
const PROV={naver_cafe:"네이버 카페",naver_kin:"지식iN",naver_blog:"네이버 블로그",daum_cafe:"다음 카페",daum_web:"다음 웹",ppomppu:"뽐뿌",copy:"복사",web_share:"공유"};
let DATA={rows:[]};
const $=s=>document.querySelector(s);
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const safeUrl=u=>/^https:\/\//.test(u||"")?u:"";
function toast(t){const e=$("#toast");e.textContent=t;e.classList.add("on");setTimeout(()=>e.classList.remove("on"),1800)}
function stOf(r){return r.removal_reason?"removed":(r.status||"skip")}
function render(){
  const rows=DATA.rows||[], today=DATA.today||"";
  const c=s=>rows.filter(r=>r.status===s).length;
  const postedToday=rows.filter(r=>r.status==="posted"&&String(r.posted_at||"").startsWith(today)).length;
  const matched=rows.filter(r=>r.draft||r.link).length;
  const passed=rows.filter(r=>String(r.allowed).toLowerCase()==="true"||Number(r.score)>=8).length;
  const rate=rows.length?Math.round(matched/rows.length*100):0;
  $("#tiles").innerHTML=[["오늘 게시",`${postedToday}<small> / 상한 ${DATA.cap}</small>`],["게시 대기",c("ready")],["사람 확인",c("needs_human")],["제외",c("skip")],["전체 발견",rows.length],["매칭률",`${rate}<small>%</small>`]]
    .map(([k,v])=>`<div class="tile"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");
  const steps=[["발견",rows.length],["매칭",matched],["초안",rows.filter(r=>r.draft).length],["점수 통과",passed]];
  const max=Math.max(1,rows.length);
  $("#flow").innerHTML=steps.map(([l,n])=>`<div class="step"><div class="l">${l}</div><div class="n">${n}</div><div class="bar" style="width:${Math.max(4,n/max*100)}%"></div></div>`).join("")
    +`<div class="step end"><div><div class="l">게시</div><div class="n" style="color:var(--pos)">${c("posted")}</div></div><div><div class="l">사람</div><div class="n" style="color:var(--warn)">${c("needs_human")+c("ready")}</div></div><div><div class="l">제외</div><div class="n">${c("skip")}</div></div></div>`;
  fill("#fProv",[...new Set(rows.map(r=>r.provider||r.channel).filter(Boolean))],v=>PROV[v]||v);
  fill("#fWhere",[...new Set(rows.map(r=>r.where).filter(Boolean))],v=>v);
  const fs=$("#fStatus").value,fp=$("#fProv").value,fw=$("#fWhere").value,q=$("#fQ").value.trim().toLowerCase();
  const list=rows.filter(r=>(!fs||r.status===fs)&&(!fp||(r.provider||r.channel)===fp)&&(!fw||r.where===fw)
    &&(!q||[r.title,r.query,r.draft,r.where].join(" ").toLowerCase().includes(q)))
    .sort((a,b)=>String(b.found_at).localeCompare(String(a.found_at)));
  $("#list").innerHTML=list.length?list.map(card).join(""):`<div class="empty">${rows.length?"조건에 맞는 후보가 없습니다.":"아직 발견된 후보가 없습니다. <code>python -m agent run --query \"…\"</code>로 찾기를 시작하세요."}</div>`;
  const f=DATA.rules||{};
  $("#foot").innerHTML=`<span>하루 게시 상한 ${DATA.cap}건</span><span>${esc(f.note||"")}</span><span>같은 카페 하루 1건 · 관계 공개 필수</span><span>마지막 갱신 ${esc(DATA.at||"")}</span>`;
}
function fill(sel,vals,label){const el=$(sel),cur=el.value,first=el.options[0].outerHTML;
  el.innerHTML=first+vals.sort().map(v=>`<option value="${esc(v)}">${esc(label(v))}</option>`).join("");el.value=vals.includes(cur)?cur:""}
function card(r){
  const s=stOf(r), dis=DEMO?"disabled":"";
  const parts=["fit","data","fresh","rule"].filter(k=>r[k]!==undefined&&r[k]!=="");
  const score=parts.length?parts.map(k=>`${k} ${esc(r[k])}`).join(" · ")+` = <b>${esc(r.score)}</b>`:`점수 <b>${esc(r.score||"-")}</b>`;
  const link=safeUrl(r.link), src=safeUrl(r.source_url), posted=safeUrl(r.posted_url);
  return `<article class="item ${s}" data-id="${esc(r.candidate_id)}">
  <div class="row"><span class="chip st-${s}">${s==="removed"?"삭제·신고":esc(ST[r.status]||r.status||"-")}</span>
   <span class="chip">${esc(PROV[r.provider||r.channel]||r.provider||r.channel||"-")}</span>
   ${r.where?`<span class="chip">${esc(r.where)}</span>`:""}<span class="score">${score}</span></div>
  <div class="title">${esc(r.title||"(제목 없음)")}</div>
  <div class="meta">검색어 ${esc(r.query||"-")} · 발견 ${esc(r.found_at||"-")}${r.placement_id?` · ${esc(r.placement_id)}`:""}${r.posted_at?` · 게시 ${esc(r.posted_at)}`:""}</div>
  ${r.why?`<div class="why">${esc(r.why)}</div>`:""}${r.removal_reason?`<div class="reason">사유: ${esc(r.removal_reason)}</div>`:""}
  ${r.draft?`<pre class="draft">${esc(r.draft)}</pre>`:""}
  ${link?`<a class="link" href="${esc(link)}" target="_blank" rel="noopener">${esc(link)}</a>`:""}
  <div class="row">${src?`<a class="btn" href="${esc(src)}" target="_blank" rel="noopener">원문 열기</a>`:""}
   ${r.draft?`<button data-act="copy">초안 복사</button>`:""}
   ${posted?`<a class="btn" href="${esc(posted)}" target="_blank" rel="noopener">게시글 보기</a>`:""}
   <button class="primary" data-act="posted" ${dis}>게시 완료 표시</button>
   <button data-act="needs_human" ${dis}>사람에게 넘김</button>
   <button data-act="skip" ${dis}>제외</button></div></article>`;
}
async function load(){try{const r=await fetch("/api/queue",{cache:"no-store"});DATA=await r.json();$("#demo").hidden=!DATA.demo;render()}catch(e){toast("불러오기 실패")}}
async function mark(id,body){const r=await fetch("/api/mark",{method:"POST",headers:{"Content-Type":"application/json","X-Admin-Token":TOKEN},body:JSON.stringify({candidate_id:id,...body})});
  const j=await r.json().catch(()=>({}));toast(j.ok?"저장했어요":(j.why||"실패했어요"));if(j.ok)load()}
document.addEventListener("click",async e=>{const b=e.target.closest("button[data-act]");if(!b)return;const it=b.closest(".item"),id=it.dataset.id,act=b.dataset.act;
  if(act==="copy"){const t=it.querySelector("pre.draft").textContent;try{await navigator.clipboard.writeText(t);toast("초안을 복사했어요")}catch(_){const s=getSelection(),rg=document.createRange();rg.selectNodeContents(it.querySelector("pre.draft"));s.removeAllRanges();s.addRange(rg);toast("선택했어요 — ⌘C로 복사")}return}
  if(DEMO)return toast("데모(읽기 전용)입니다");
  if(act==="posted"){const url=await ask("게시한 글(댓글) 주소를 붙여 넣으세요 (https://…)");if(url)mark(id,{status:"posted",posted_url:url});return}
  if(act==="skip"){const why=await ask("제외 사유 (예: 홍보 금지 게시판, 질문과 무관)");if(why!==null)mark(id,{status:"skip",removal_reason:why});return}
  mark(id,{status:act})});
function ask(label){return new Promise(res=>{const d=document.createElement("div");d.className="item";d.style.cssText="position:fixed;inset:auto 16px 16px 16px;max-width:560px;margin:0 auto;z-index:9;box-shadow:0 8px 30px rgba(0,0,0,.18)";
  d.innerHTML=`<label class="meta" for="askIn">${esc(label)}</label><input id="askIn" type="search" style="width:100%"><div class="row"><button class="primary" id="askOk">저장</button><button id="askNo">취소</button></div>`;
  document.body.appendChild(d);const i=d.querySelector("#askIn");i.focus();const done=v=>{d.remove();res(v)};
  d.querySelector("#askOk").onclick=()=>done(i.value.trim());d.querySelector("#askNo").onclick=()=>done(null);i.onkeydown=e=>{if(e.key==="Enter")done(i.value.trim());if(e.key==="Escape")done(null)}})}
["#fStatus","#fProv","#fWhere"].forEach(s=>$(s).addEventListener("change",render));$("#fQ").addEventListener("input",render);
load();setInterval(load,10000);
</script></body></html>"""


def main() -> None:
    global DEMO
    ap = argparse.ArgumentParser(prog="python -m agent.admin")
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--demo", action="store_true", help="examples/queue.sample.csv를 읽기 전용으로 보여준다")
    a = ap.parse_args()
    DEMO = a.demo
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    print(f"GTM 관리자 http://127.0.0.1:{a.port}  ({'데모·읽기 전용' if DEMO else '큐 ' + queue.PATH})  — Ctrl+C로 종료")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
