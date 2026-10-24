"""GTM Agent 관리자 — 발견부터 게시까지 전 과정을 한 화면에서 보고 조작한다. **로컬 전용**.

실행:
  python -m agent.admin                 # data/queue.csv (GTM_QUEUE로 바꿀 수 있다)
  python -m agent.admin --demo          # examples/queue.sample.csv 읽기 전용
  python -m agent.admin --port 8800

- 127.0.0.1에만 바인드한다. 외부 의존성 없음(표준 라이브러리). 글꼴·아이콘도 외부에서 받지 않는다.
- 시작할 때 무작위 토큰을 만들어 페이지에 심는다. POST는 X-Admin-Token이 맞을 때만 받는다(CSRF 방지).
- 게시 자체는 하지 않는다. 사람이 게시한 뒤 '게시 완료 표시'로 기록하거나, 제외·사람 확인으로 상태를 바꾼다.
- 디자인은 리블 디자인 시스템을 따른다(frontend/app/globals.css 토큰·타이포 역할, common/Chip 규약,
  common/CardHead 카드 머리). 색은 :root 토큰으로만 쓰고 다크 모드는 토큰만 바꾼다.
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
PROGRESS = Path(os.environ.get("GTM_PROGRESS", "data/run_status.json"))
NAVER_CALLS = Path(os.environ.get("GTM_NAVER_CALLS", "data/naver_calls.json"))
NAVER_CAP = int(os.environ.get("GTM_NAVER_DAILY_CAP", "300"))
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
        return {"cafes": 0, "note": "허용 카페 없음 — 자동 게시 꺼짐"}
    try:
        d = json.loads(RULES.read_text(encoding="utf-8"))
    except Exception:
        return {"cafes": 0, "note": "rules.json을 읽지 못함 — 자동 게시 꺼짐"}
    n = sum(1 for ch in d.values() if isinstance(ch, dict) for v in ch.values() if isinstance(v, dict) and v.get("allowed"))
    return {"cafes": n, "note": f"허용 카페 {n}곳" if n else "허용 카페 없음 — 자동 게시 꺼짐"}


def _today() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def naver_info() -> dict:
    try:
        d = json.loads(NAVER_CALLS.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    return {"today": int(d.get(_today(), 0)), "cap": NAVER_CAP}


def status() -> dict:
    """실행 진행 상황(pipeline이 data/run_status.json에 쓴다) + 안전장치 숫자."""
    run = None
    if DEMO:
        rs = rows()
        run = {"running": False, "total": len(rs), "processed": len(rs), "updated_at": "예시 데이터",
               "queries": sorted({r.get("query", "") for r in rs if r.get("query")})}
    elif PROGRESS.exists():
        try:
            run = json.loads(PROGRESS.read_text(encoding="utf-8"))
        except Exception:
            run = None
    return {"run": run, "naver": naver_info(), "rules": rules_info(), "cap": DAILY_CAP, "demo": DEMO,
            "at": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")}


def payload() -> dict:
    return {"rows": rows(), "demo": DEMO, "cap": DAILY_CAP, "rules": rules_info(), "naver": naver_info(),
            "today": _today(), "at": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")}


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
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            html = PAGE.replace("__TOKEN__", TOKEN).replace("__DEMO__", "true" if DEMO else "false")
            return self._send(200, html.encode(), "text/html; charset=utf-8")
        if path == "/api/queue":
            return self._json(200, payload())
        if path == "/api/status":
            return self._json(200, status())
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
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>리블 GTM Agent</title>
<style>
/* 리블 디자인 시스템 — frontend/app/globals.css 값 그대로(라이트/다크). 컴포넌트는 토큰만 쓴다. */
:root{
  --app:#f7f8fa;--bg:#ffffff;--hover:#f4f5f7;--chip:#f2f4f6;--line:#eff1f3;--line-strong:#e3e6ea;
  --ink:#191f28;--ink2:#4e5968;--sub:#616c7a;--dim:#6d7581;
  --brand:#3182f6;--brand-strong:#2272eb;--brand-soft:#eaf3ff;--on-brand:#ffffff;
  --up:#f04452;--up-soft:#feebec;--live:#3182f6;
  --pos:#12b886;--pos-soft:#e6f7f1;--pos-strong:#0b7a5a;--warn:#e8a33d;--warn-soft:#fbf1e0;--warn-strong:#a15c00;
  --radius-sm:6px;--radius-md:8px;--radius-lg:10px;--radius-xl:12px;--radius-2xl:16px;
  --font:'Pretendard Variable',Pretendard,-apple-system,'Apple SD Gothic Neo',system-ui,sans-serif;
  color-scheme:light;
}
@media (prefers-color-scheme:dark){:root{
  --app:#0e0f11;--bg:#16171a;--hover:#232529;--chip:#232529;--line:#282a2d;--line-strong:#333640;
  --ink:#edeef1;--ink2:#c7ccd4;--sub:#aab1bc;--dim:#79818d;
  --brand:#4d9fff;--brand-strong:#4d9fff;--brand-soft:#16233a;--on-brand:#ffffff;
  --up:#ff5c6c;--up-soft:#2b1a1f;--live:#4d9fff;
  --pos:#2dd4a0;--pos-soft:#122a24;--pos-strong:#34d8a6;--warn:#f0b254;--warn-soft:#2e2413;--warn-strong:#f0b254;
  color-scheme:dark;
}}
*{box-sizing:border-box}
html,body{margin:0;background:var(--app);color:var(--ink2);font-family:var(--font);-webkit-font-smoothing:antialiased;letter-spacing:-0.01em}
a{color:inherit;text-decoration:none}
button,input,select{font:inherit;color:inherit}
:focus-visible{outline:2px solid var(--brand);outline-offset:2px;border-radius:var(--radius-md)}
.num{font-variant-numeric:tabular-nums}
/* 타이포 역할(globals.css t-*) */
.t-display{font-size:30px;line-height:1.14;letter-spacing:-0.02em}
.t-headline{font-size:17px;line-height:1.28;letter-spacing:-0.015em}
.t-title{font-size:15px;line-height:1.3;letter-spacing:-0.01em}
.t-body{font-size:14px;line-height:1.58;letter-spacing:-0.005em}
.t-label{font-size:13px;line-height:1.3}
.t-cap{font-size:12.5px;line-height:1.3}
.t-eyebrow{font-size:12px;line-height:1.26;letter-spacing:0.02em}
.t-micro{font-size:11px;line-height:1.2;letter-spacing:0.01em}
.t-val-xl{font-size:30px;line-height:1.12;letter-spacing:-0.02em;font-variant-numeric:tabular-nums}
.t-val{font-size:15px;line-height:1.2;letter-spacing:-0.01em;font-variant-numeric:tabular-nums}
@media (max-width:767px){
  .t-headline{font-size:18px}.t-title{font-size:17px}.t-body{font-size:16px}.t-label{font-size:14.5px}
  .t-cap{font-size:14px}.t-eyebrow{font-size:13px}.t-micro{font-size:12.5px}.t-val-xl{font-size:32px}.t-val{font-size:16px}
}
.b6{font-weight:600}.b7{font-weight:700}.b8{font-weight:800}
.ink{color:var(--ink)}.sub{color:var(--sub)}.dim{color:var(--dim)}
/* 레이아웃 */
.wrap{max-width:1080px;margin:0 auto;padding:20px max(16px,env(safe-area-inset-left)) 48px max(16px,env(safe-area-inset-right));display:flex;flex-direction:column;gap:12px}
.card{background:var(--bg);border-radius:var(--radius-2xl);padding:16px 20px}
@media (max-width:767px){.card{padding:16px}}
/* 카드 머리 — common/CardHead: t-headline extrabold ink + t-cap dim 부제, 오른쪽 컨트롤 */
.head{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:6px 8px;margin-bottom:12px}
.head>div:first-child{min-width:0;flex:1 1 160px}
.head h2{margin:0;font-size:17px;line-height:1.28;letter-spacing:-0.015em;font-weight:800;color:var(--ink)}
.head p{margin:2px 0 0;color:var(--dim)}
/* 카드 밖 컨트롤 바 — common/CardBar */
.bar{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px;min-height:36px;min-width:0}
/* 칩 — common/Chip: rounded-full t-cap bold, 꺼짐 = chip/sub, 켜짐 = ink 반전(기본) 또는 brand-soft/brand */
.chip{display:inline-flex;align-items:center;gap:4px;white-space:nowrap;border:0;border-radius:999px;padding:4px 10px;background:var(--chip);color:var(--sub);cursor:pointer}
.chip:hover{background:var(--hover)}
.chip[aria-pressed=true]{background:var(--ink);color:var(--bg)}
.chip .n{opacity:.72;font-weight:600}
@media (max-width:767px){.chip{padding:6px 12px}}
/* 상태 표시(배지) — 색 + 글자. 조작이 아니라 상태라 Chip과 구분한다 */
.badge{display:inline-flex;align-items:center;gap:4px;border-radius:var(--radius-sm);padding:3px 8px;font-weight:700;white-space:nowrap}
.s-needs_human{background:var(--warn-soft);color:var(--warn-strong)}
.s-ready{background:var(--brand-soft);color:var(--brand)}
.s-posted{background:var(--pos-soft);color:var(--pos-strong)}
.s-skip{background:var(--chip);color:var(--sub)}
.s-removed{background:var(--up-soft);color:var(--up)}
.tag{display:inline-flex;align-items:center;gap:4px;color:var(--sub);white-space:nowrap;max-width:100%;overflow:hidden;text-overflow:ellipsis}
/* 버튼 */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;min-height:36px;padding:0 14px;border-radius:var(--radius-xl);border:0;background:var(--chip);color:var(--ink);font-weight:700;cursor:pointer;white-space:nowrap}
.btn:hover{background:var(--hover)}
.btn.primary{background:var(--brand);color:var(--on-brand)}
.btn.primary:hover{background:var(--brand-strong)}
.btn.ghost{background:transparent;color:var(--sub)}
.btn.ghost:hover{background:var(--hover);color:var(--ink)}
.btn[disabled]{opacity:.45;cursor:not-allowed}
.ico{width:16px;height:16px;flex:none;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
/* 머리 */
.top{display:flex;align-items:flex-end;justify-content:space-between;gap:12px;flex-wrap:wrap;padding:4px 2px 4px}
.top h1{margin:2px 0 0;color:var(--ink);font-weight:800}
.mark{display:inline-flex;align-items:center;gap:6px;color:var(--brand);font-weight:700}
.mark i{width:8px;height:8px;border-radius:2px;background:var(--brand);display:inline-block}
.demo{background:var(--warn-soft);color:var(--warn-strong)}
/* 실행 상태 */
.run{display:flex;flex-direction:column;gap:10px}
.run-line{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.dot{width:8px;height:8px;border-radius:50%;background:var(--dim);flex:none}
.dot.on{background:var(--live);box-shadow:0 0 0 0 color-mix(in srgb,var(--live) 50%,transparent);animation:pulse 1.6s infinite}
.dot.done{background:var(--pos)}
@keyframes pulse{70%{box-shadow:0 0 0 8px transparent}100%{box-shadow:0 0 0 0 transparent}}
@media (prefers-reduced-motion:reduce){.dot.on{animation:none}}
.track{height:6px;border-radius:99px;background:var(--chip);overflow:hidden}
.track>i{display:block;height:100%;background:var(--brand);border-radius:99px;transition:width .4s}
.now{min-width:0;flex:1 1 220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cmd{display:flex;align-items:center;gap:8px;background:var(--app);border-radius:var(--radius-lg);padding:8px 8px 8px 12px;min-width:0}
.cmd code{flex:1;min-width:0;overflow-x:auto;white-space:nowrap;font-family:ui-monospace,Menlo,monospace;font-size:12.5px;color:var(--ink)}
/* 요약 타일 */
.tiles{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:8px}
@media (max-width:900px){.tiles{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media (max-width:520px){.tiles{grid-template-columns:repeat(2,minmax(0,1fr))}}
.tile{background:var(--bg);border-radius:var(--radius-2xl);padding:14px 16px;display:flex;flex-direction:column;gap:6px;min-width:0}
.tile .v{color:var(--ink);font-weight:800}
.tile .v small{font-size:13px;font-weight:600;color:var(--dim);margin-left:2px}
.tile.warn .v{color:var(--warn-strong)}.tile.brand .v{color:var(--brand)}.tile.pos .v{color:var(--pos-strong)}
/* 퍼널 */
.funnel{display:flex;flex-direction:column;gap:10px}
.fl{display:grid;grid-template-columns:96px minmax(0,1fr) 56px;align-items:center;gap:10px}
.fl .track{height:10px}
.fl.last .track>i{background:var(--pos)}
.reasons{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px;padding-top:12px;border-top:1px solid var(--line)}
/* 필터 */
.filters{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.selects{display:flex;gap:8px;flex-wrap:wrap;align-items:center;flex:1 1 420px;justify-content:flex-end;min-width:0}
.sel{max-width:200px;min-width:0;width:auto;text-overflow:ellipsis}
.sel,.search{min-height:36px;border-radius:var(--radius-xl);border:1px solid var(--line-strong);background:var(--bg);padding:0 10px;color:var(--ink)}
.search{display:flex;align-items:center;gap:6px;padding:0 10px;min-width:0;flex:0 1 260px;height:36px}
@media (max-width:767px){.selects{justify-content:stretch;flex-basis:100%}.sel{flex:1 1 0;max-width:none;width:0}.search{flex:1 1 100%}}
.search input{border:0;outline:0;background:transparent;min-width:0;flex:1;height:34px;color:var(--ink)}
/* 후보 카드 */
.list{display:flex;flex-direction:column;gap:10px}
.item{background:var(--bg);border-radius:var(--radius-2xl);padding:16px 20px;display:flex;flex-direction:column;gap:10px;min-width:0}
@media (max-width:767px){.item{padding:16px}}
.item-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap;justify-content:space-between}
.item-head .l{display:flex;align-items:center;gap:8px;flex-wrap:wrap;min-width:0}
.score{display:flex;align-items:center;gap:10px}
.score .total{color:var(--ink);font-weight:800}
.bars{display:flex;gap:8px}
.sb{display:flex;flex-direction:column;gap:3px;align-items:flex-start}
.sb .t{display:flex;gap:2px}
.sb .t i{width:8px;height:4px;border-radius:2px;background:var(--chip)}
.sb .t i.on{background:var(--brand)}
.sb.rule .t i.on{background:var(--pos)}
.title{color:var(--ink);font-weight:700;display:inline-flex;gap:6px;align-items:flex-start;word-break:keep-all;overflow-wrap:anywhere}
.title:hover{color:var(--brand)}
.title .ico{margin-top:2px;color:var(--dim)}
.meta{display:flex;flex-wrap:wrap;gap:4px 12px;color:var(--dim)}
.why{display:flex;gap:6px;align-items:flex-start;color:var(--warn-strong)}
.why.neg{color:var(--up)}
.match{display:flex;align-items:center;gap:8px;min-width:0;background:var(--app);border-radius:var(--radius-lg);padding:8px 10px}
.match .kind{flex:none;background:var(--brand-soft);color:var(--brand);border-radius:var(--radius-sm);padding:2px 6px;font-weight:700}
.match a{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--ink2)}
.match a:hover{color:var(--brand)}
.draft{margin:0;font-family:var(--font);white-space:pre-wrap;word-break:keep-all;overflow-wrap:anywhere;color:var(--ink);background:var(--app);border-left:3px solid var(--line-strong);border-radius:0 var(--radius-lg) var(--radius-lg) 0;padding:12px 14px}
.acts{display:flex;gap:8px;flex-wrap:wrap}
@media (max-width:520px){.acts .btn{flex:1 1 calc(50% - 4px)}}
/* 제외 접힘 */
.fold{display:flex;align-items:center;justify-content:center;gap:6px;width:100%;min-height:44px;border:0;border-radius:var(--radius-2xl);background:var(--bg);color:var(--sub);font-weight:700;cursor:pointer}
.fold:hover{color:var(--ink)}
.mini{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:8px;align-items:center;padding:10px 0;border-top:1px solid var(--line)}
.mini:first-child{border-top:0}
.mini .tt{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--ink2)}
.empty{background:var(--bg);border-radius:var(--radius-2xl);padding:28px 20px;text-align:center;color:var(--sub)}
/* 안전장치 */
.safety{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}
@media (max-width:767px){.safety{grid-template-columns:1fr}}
.sf{display:flex;gap:10px;align-items:flex-start;min-width:0}
.sf .ic{width:32px;height:32px;border-radius:var(--radius-lg);background:var(--app);display:grid;place-items:center;color:var(--sub);flex:none}
.sf .ic.ok{color:var(--pos-strong);background:var(--pos-soft)}.sf .ic.off{color:var(--warn-strong);background:var(--warn-soft)}
.sf .track{margin-top:6px;height:4px}
/* 시트·토스트 */
.toast{position:fixed;left:50%;bottom:calc(20px + env(safe-area-inset-bottom));transform:translate(-50%,16px);opacity:0;pointer-events:none;background:var(--ink);color:var(--bg);padding:10px 16px;border-radius:var(--radius-xl);font-weight:600;transition:.2s;z-index:20;max-width:calc(100vw - 32px)}
.toast.on{opacity:1;transform:translate(-50%,0)}
.scrim{position:fixed;inset:0;background:color-mix(in srgb,#000 32%,transparent);display:grid;place-items:end center;z-index:30;padding:16px}
@media (min-width:768px){.scrim{place-items:center}}
.sheet{background:var(--bg);border-radius:var(--radius-2xl);padding:20px;width:100%;max-width:480px;display:flex;flex-direction:column;gap:12px}
.sheet input{min-height:44px;border-radius:var(--radius-xl);border:1px solid var(--line-strong);background:var(--bg);padding:0 12px;color:var(--ink)}
.sheet input:focus{border-color:var(--brand);outline:0}
.sheet .acts{justify-content:flex-end}
[hidden]{display:none!important}
</style></head><body>
<svg width="0" height="0" style="position:absolute" aria-hidden="true">
 <symbol id="i-copy" viewBox="0 0 24 24"><rect width="14" height="14" x="8" y="8" rx="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></symbol>
 <symbol id="i-ext" viewBox="0 0 24 24"><path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/></symbol>
 <symbol id="i-check" viewBox="0 0 24 24"><path d="M20 6 9 17l-5-5"/></symbol>
 <symbol id="i-user" viewBox="0 0 24 24"><circle cx="12" cy="8" r="5"/><path d="M20 21a8 8 0 0 0-16 0"/></symbol>
 <symbol id="i-x" viewBox="0 0 24 24"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></symbol>
 <symbol id="i-search" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></symbol>
 <symbol id="i-link" viewBox="0 0 24 24"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></symbol>
 <symbol id="i-down" viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></symbol>
 <symbol id="i-alert" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M12 8v4"/><path d="M12 16h.01"/></symbol>
 <symbol id="i-shield" viewBox="0 0 24 24"><path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><path d="m9 12 2 2 4-4"/></symbol>
 <symbol id="i-clock" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></symbol>
 <symbol id="i-radar" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/><path d="M11 8v6"/><path d="M8 11h6"/></symbol>
</svg>
<div class="wrap">
 <header class="top">
  <div><div class="mark t-eyebrow"><i></i>리블 · 로컬 관리자 127.0.0.1</div><h1 class="t-display">GTM Agent</h1></div>
  <div class="bar"><span class="badge demo t-cap" id="demo" hidden>예시 데이터 · 읽기 전용</span><span class="t-cap dim num" id="at"></span></div>
 </header>

 <section class="card run" id="run" aria-live="polite"></section>
 <section class="tiles" id="tiles"></section>

 <section class="card">
  <div class="head"><div><h2>처리 흐름</h2><p class="t-cap">찾은 글이 어디서 걸러지고 어디로 가는지</p></div></div>
  <div class="funnel" id="funnel"></div>
  <div class="reasons" id="reasons"></div>
 </section>

 <div class="bar">
  <div class="filters" role="group" aria-label="상태" id="fStatus"></div>
  <div class="selects">
   <select class="sel t-label" id="fProv" aria-label="채널"><option value="">모든 채널</option></select>
   <select class="sel t-label" id="fWhere" aria-label="카페"><option value="">모든 카페</option></select>
   <label class="search t-label"><svg class="ico dim"><use href="#i-search"/></svg><input id="fQ" type="search" placeholder="제목·검색어·초안에서 찾기" aria-label="검색"></label>
  </div>
 </div>
 <section class="list" id="list"></section>
 <section id="skipWrap"></section>

 <section class="card">
  <div class="head"><div><h2>안전장치</h2><p class="t-cap">자동 게시가 넘지 않는 선</p></div></div>
  <div class="safety" id="safety"></div>
 </section>
</div>
<div class="toast t-label" id="toast" role="status"></div>
<script>
const TOKEN="__TOKEN__", DEMO=__DEMO__;
const ST={needs_human:"사람 확인",ready:"게시 대기",posted:"게시됨",skip:"제외",removed:"삭제·신고"};
const PROV={naver_cafe:"네이버 카페",naver_kin:"지식iN",naver_blog:"네이버 블로그",daum_cafe:"다음 카페",daum_web:"다음 웹",ppomppu:"뽐뿌",copy:"복사",web_share:"공유"};
const WHY={not_question:"질문 글 아님",low_score:"내용 점수 미달",no_match:"리블 페이지 매칭 없음",same_cafe_today:"같은 카페 오늘 이미",error:"조회 오류",same_draft:"같은 문구",dup:"중복"};
const KIND={compare:"비교",complex:"단지",hub:"허브",housing:"유형",screener:"조건"};
const ORDER={needs_human:0,ready:1,posted:2,removed:3,skip:4};
let DATA={rows:[]}, STATUS=null, FS="", SHOW_SKIP=false;
const $=s=>document.querySelector(s);
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const safeUrl=u=>/^https:\/\//.test(u||"")?u:"";
const ic=(id,cls="")=>`<svg class="ico ${cls}" aria-hidden="true"><use href="#i-${id}"/></svg>`;
const fmt=n=>Number(n||0).toLocaleString("ko-KR");
function toast(t){const e=$("#toast");e.textContent=t;e.classList.add("on");clearTimeout(e._t);e._t=setTimeout(()=>e.classList.remove("on"),1800)}
/* 삭제·신고 = 게시된 뒤 사유가 붙은 것. 파이프라인 제외 사유(not_question 등)는 그냥 '제외'다 */
function stOf(r){if(r.status==="posted"&&r.removal_reason)return "removed";if(r.status==="skip"&&/삭제|신고|경고/.test(r.removal_reason||""))return "removed";return r.status||"skip"}
function linkKind(u){const m=/utm_content=([a-z]+)/.exec(u||"");return KIND[m&&m[1]]||"페이지"}
function linkLabel(u){try{const x=new URL(u);let p=decodeURIComponent(x.pathname);if(x.pathname==="/share/compare"){const c=(x.searchParams.get("compare")||"").split("!").map(s=>s.split("|")[1]||s).filter(Boolean);return "비교 · "+c.join(" vs ")}
  const seg=p.split("/").filter(Boolean);if(seg[0]==="complex")return "단지 · "+(seg[1]||"").split("|")[1];return seg.join(" · ")}catch(_){return u}}

function renderRun(){
  const s=STATUS||{}, r=s.run;
  if(!r){$("#run").innerHTML=`<div class="run-line"><span class="dot"></span><span class="t-title b7 ink">아직 실행 기록이 없어요</span></div>
   <p class="t-cap sub" style="margin:0">아래 명령으로 찾기를 시작하면, 진행 상황이 여기에 2초마다 보여요.</p>
   <div class="cmd"><code id="cmdText">python -m agent run --qa --query "송파 리모델링 단지 비교"</code><button class="btn ghost t-label" data-copy="#cmdText">${ic("copy")}복사</button></div>`;return}
  const tot=Number(r.total||0), done=Number(r.processed||0), pct=tot?Math.min(100,Math.round(done/tot*100)):0, sm=r.summary||{};
  const when=String(r.updated_at||"").slice(11,16)||String(r.updated_at||"");
  const q=(r.queries||[]).slice(0,5).map(x=>`<span class="tag t-cap">#${esc(x)}</span>`).join("");
  $("#run").innerHTML=`<div class="run-line">
    <span class="dot ${r.running?"on":"done"}"></span>
    <span class="t-title b7 ink">${r.running?"실행 중":"마지막 실행"}</span>
    <span class="t-val b7 ink num">${fmt(done)}<span class="dim b6"> / ${fmt(tot)}</span></span>
    ${r.running?`<span class="now t-cap sub">${r.provider?esc(PROV[r.provider]||r.provider)+" · ":""}${esc(r.current||"")}</span>`:`<span class="t-cap dim num">${esc(when)}</span>`}
   </div>
   <div class="track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}"><i style="width:${r.running?pct:100}%"></i></div>
   <div class="run-line t-cap dim">${q}${sm.found!==undefined?`<span class="num">찾음 ${fmt(sm.found)} · 매칭 ${fmt(sm.matched)} · 사람 ${fmt(sm.needs_human)} · 제외 ${fmt(sm.skip)}</span>`:""}</div>`;
}

function render(){
  const rows=DATA.rows||[], today=DATA.today||"";
  const by={};rows.forEach(r=>{const s=stOf(r);by[s]=(by[s]||0)+1});
  const postedToday=rows.filter(r=>r.status==="posted"&&String(r.posted_at||"").startsWith(today)).length;
  const reason={};rows.filter(r=>r.status==="skip").forEach(r=>{const k=r.removal_reason||"기타";reason[k]=(reason[k]||0)+1});
  const notQ=reason.not_question||0;
  const matched=rows.filter(r=>r.draft).length;
  const passed=rows.filter(r=>["ready","needs_human","posted"].includes(r.status)).length;
  const rate=rows.length?Math.round(matched/rows.length*100):0;
  $("#at").textContent="갱신 "+String(DATA.at||"").slice(11,19);
  $("#tiles").innerHTML=[
    ["오늘 게시",`${postedToday}<small>/ ${DATA.cap}</small>`,"pos"],
    ["사람 확인",fmt(by.needs_human||0),"warn"],["게시 대기",fmt(by.ready||0),"brand"],
    ["제외",fmt(by.skip||0),""],["발견",fmt(rows.length),""],["매칭률",`${rate}<small>%</small>`,""]]
    .map(([k,v,t])=>`<div class="tile ${t}"><span class="t-label sub">${k}</span><span class="v t-val-xl">${v}</span></div>`).join("");
  const max=Math.max(1,rows.length);
  const steps=[["발견",rows.length],["질문 글",rows.length-notQ],["매칭",matched],["점수 통과",passed],["사람·게시",(by.needs_human||0)+(by.ready||0)+(by.posted||0)]];
  $("#funnel").innerHTML=steps.map(([l,n],i)=>`<div class="fl ${i===steps.length-1?"last":""}"><span class="t-label sub">${l}</span>
    <div class="track"><i style="width:${n?Math.max(2,n/max*100):0}%"></i></div><span class="t-val b7 ink num" style="text-align:right">${fmt(n)}</span></div>`).join("");
  const rs=Object.entries(reason).sort((a,b)=>b[1]-a[1]);
  $("#reasons").innerHTML=rs.length?`<span class="t-cap dim" style="align-self:center">제외 사유</span>`+rs.map(([k,n])=>`<span class="badge s-skip t-cap">${esc(WHY[k]||k)} <span class="num">${fmt(n)}</span></span>`).join(""):`<span class="t-cap dim">제외된 글이 없어요</span>`;
  // 상태 칩 — Chip 규약: 켜짐 = ink 반전
  const chips=[["","전체",rows.length],["needs_human","사람 확인",by.needs_human||0],["ready","게시 대기",by.ready||0],["posted","게시됨",by.posted||0],["removed","삭제·신고",by.removed||0],["skip","제외",by.skip||0]];
  $("#fStatus").innerHTML=chips.filter(([k,,n])=>k===""||n||k===FS).map(([k,l,n])=>`<button type="button" class="chip t-cap b7" data-fs="${k}" aria-pressed="${FS===k}">${l} <span class="n num">${fmt(n)}</span></button>`).join("");
  fill("#fProv",[...new Set(rows.map(r=>r.provider||r.channel).filter(Boolean))],v=>PROV[v]||v);
  fill("#fWhere",[...new Set(rows.map(r=>r.where).filter(Boolean))],v=>v);
  const fp=$("#fProv").value,fw=$("#fWhere").value,q=$("#fQ").value.trim().toLowerCase();
  const pass=r=>(!fp||(r.provider||r.channel)===fp)&&(!fw||r.where===fw)&&(!q||[r.title,r.query,r.draft,r.where].join(" ").toLowerCase().includes(q));
  const sorter=(a,b)=>(ORDER[stOf(a)]-ORDER[stOf(b)])||(Number(b.score||0)-Number(a.score||0))||String(b.found_at).localeCompare(String(a.found_at));
  const main=rows.filter(r=>pass(r)&&(FS?stOf(r)===FS:stOf(r)!=="skip")).sort(sorter);
  $("#list").innerHTML=main.length?main.map(card).join(""):`<div class="empty t-body">${rows.length?(FS==="skip"?"":"조건에 맞는 후보가 없어요."):"아직 찾은 글이 없어요. 위 명령으로 찾기를 시작하세요."}</div>`;
  const skips=FS?[]:rows.filter(r=>pass(r)&&stOf(r)==="skip").sort(sorter);
  $("#skipWrap").innerHTML=skips.length?`<button class="fold t-label" id="foldBtn" aria-expanded="${SHOW_SKIP}">${SHOW_SKIP?"제외 접기":`제외 ${fmt(skips.length)}건 보기`} ${ic("down")}</button>
    ${SHOW_SKIP?`<div class="card" style="margin-top:10px">${skips.slice(0,300).map(r=>`<div class="mini"><span class="badge s-skip t-micro">${esc(WHY[r.removal_reason]||r.removal_reason||"제외")}</span>
      <a class="tt t-label" href="${esc(safeUrl(r.source_url))}" target="_blank" rel="noopener">${esc(r.title||"(제목 없음)")}</a><span class="t-micro dim">${esc(PROV[r.provider||r.channel]||"")}</span></div>`).join("")}</div>`:""}`:"";
  renderSafety();
}
function renderSafety(){
  const s=STATUS||{}, nv=(s.naver||DATA.naver||{today:0,cap:300}), rl=(s.rules||DATA.rules||{cafes:0,note:""});
  const rows=DATA.rows||[], today=DATA.today||"", posted=rows.filter(r=>r.status==="posted"&&String(r.posted_at||"").startsWith(today)).length;
  const cap=DATA.cap||3, np=nv.cap?Math.min(100,Math.round(nv.today/nv.cap*100)):0;
  $("#safety").innerHTML=`
   <div class="sf"><span class="ic">${ic("clock")}</span><div style="min-width:0;flex:1"><div class="t-label b7 ink">하루 게시 상한</div><div class="t-cap sub num">오늘 ${posted} / ${cap}건 · 같은 카페 하루 1건</div><div class="track"><i style="width:${Math.min(100,posted/cap*100)}%"></i></div></div></div>
   <div class="sf"><span class="ic ${rl.cafes?"ok":"off"}">${ic("shield")}</span><div style="min-width:0"><div class="t-label b7 ink">자동 게시 허용 카페</div><div class="t-cap sub">${esc(rl.note||"")}</div><div class="t-micro dim" style="margin-top:4px">카페 공지에서 홍보·링크 규칙을 확인한 곳만</div></div></div>
   <div class="sf"><span class="ic">${ic("radar")}</span><div style="min-width:0;flex:1"><div class="t-label b7 ink">네이버 검색 호출</div><div class="t-cap sub num">오늘 ${fmt(nv.today)} / ${fmt(nv.cap)}회</div><div class="track"><i style="width:${np}%"></i></div></div></div>`;
}
function fill(sel,vals,label){const el=$(sel),cur=el.value,first=el.options[0].outerHTML;
  el.innerHTML=first+vals.sort().map(v=>`<option value="${esc(v)}">${esc(label(v))}</option>`).join("");el.value=vals.includes(cur)?cur:""}
function scoreBars(r){
  const parts=[["적합","fit",3],["데이터","data",3],["최신","fresh",2],["규칙","rule",2]];
  if(!parts.some(([,k])=>r[k]!==undefined&&r[k]!==""))return "";
  return `<div class="bars" aria-label="점수 분해">${parts.map(([l,k,mx])=>{const v=Number(r[k]||0);
    return `<div class="sb ${k}"><span class="t">${Array.from({length:mx},(_,i)=>`<i class="${i<v?"on":""}"></i>`).join("")}</span><span class="t-micro dim">${l} ${v}</span></div>`}).join("")}</div>`;
}
function card(r){
  const s=stOf(r), dis=DEMO?"disabled":"";
  const link=safeUrl(r.link), src=safeUrl(r.source_url), posted=safeUrl(r.posted_url);
  const whyTxt=r.why||(r.status==="skip"?(WHY[r.removal_reason]||r.removal_reason):"");
  return `<article class="item" data-id="${esc(r.candidate_id)}">
  <div class="item-head"><div class="l">
    <span class="badge s-${s} t-cap">${esc(ST[s]||s)}</span>
    <span class="tag t-cap b6">${esc(PROV[r.provider||r.channel]||r.provider||r.channel||"-")}</span>
    ${r.where?`<span class="tag t-cap">${esc(r.where)}</span>`:""}</div>
   <div class="score">${scoreBars(r)}<span class="total t-val num">${esc(r.score||"-")}<span class="t-micro dim b6">점</span></span></div></div>
  ${src?`<a class="title t-title" href="${esc(src)}" target="_blank" rel="noopener">${esc(r.title||"(제목 없음)")}${ic("ext")}</a>`:`<span class="title t-title">${esc(r.title||"(제목 없음)")}</span>`}
  <div class="meta t-cap"><span>검색어 ${esc(r.query||"-")}</span><span class="num">발견 ${esc(String(r.found_at||"-").slice(5))}</span>${r.placement_id?`<span class="num">${esc(r.placement_id)}</span>`:""}${r.posted_at?`<span class="num">게시 ${esc(r.posted_at)}</span>`:""}</div>
  ${whyTxt?`<div class="why t-cap ${s==="removed"?"neg":""}">${ic("alert")}<span>${esc(whyTxt)}</span></div>`:""}
  ${link?`<div class="match t-cap"><span class="kind t-micro">${esc(linkKind(link))}</span>${ic("link","dim")}<a href="${esc(link)}" target="_blank" rel="noopener" title="${esc(link)}">${esc(linkLabel(link))}</a></div>`:""}
  ${r.draft?`<pre class="draft t-body">${esc(r.draft)}</pre>`:""}
  ${r.status==="skip"?"":`<div class="acts">
   ${r.draft?`<button class="btn primary t-label" data-act="copy">${ic("copy")}초안 복사</button>`:""}
   ${src?`<a class="btn t-label" href="${esc(src)}" target="_blank" rel="noopener">${ic("ext")}원문 열기</a>`:""}
   ${posted?`<a class="btn t-label" href="${esc(posted)}" target="_blank" rel="noopener">${ic("ext")}게시글 보기</a>`:""}
   ${r.status!=="posted"?`<button class="btn t-label" data-act="posted" ${dis}>${ic("check")}게시 완료 표시</button>`:""}
   ${r.status!=="needs_human"&&r.status!=="posted"?`<button class="btn ghost t-label" data-act="needs_human" ${dis}>${ic("user")}사람에게</button>`:""}
   <button class="btn ghost t-label" data-act="skip" ${dis}>${ic("x")}제외</button></div>`}
  </article>`;
}
async function load(){try{const r=await fetch("/api/queue",{cache:"no-store"});DATA=await r.json();$("#demo").hidden=!DATA.demo;render()}catch(e){toast("불러오지 못했어요")}}
async function loadStatus(){try{const r=await fetch("/api/status",{cache:"no-store"});const s=await r.json();const was=STATUS&&STATUS.run&&STATUS.run.running;STATUS=s;renderRun();renderSafety();
  if(s.run&&(s.run.running||was))load()}catch(e){}}
async function mark(id,body){const r=await fetch("/api/mark",{method:"POST",headers:{"Content-Type":"application/json","X-Admin-Token":TOKEN},body:JSON.stringify({candidate_id:id,...body})});
  const j=await r.json().catch(()=>({}));toast(j.ok?"저장했어요":(j.why||"저장하지 못했어요"));if(j.ok)load()}
async function copyText(t,node){try{await navigator.clipboard.writeText(t);toast("복사했어요")}catch(_){const s=getSelection(),rg=document.createRange();rg.selectNodeContents(node);s.removeAllRanges();s.addRange(rg);toast("선택했어요 — ⌘C로 복사하세요")}}
document.addEventListener("click",async e=>{
  const f=e.target.closest("[data-fs]");if(f){FS=f.dataset.fs;render();return}
  if(e.target.closest("#foldBtn")){SHOW_SKIP=!SHOW_SKIP;render();return}
  const c=e.target.closest("[data-copy]");if(c){const n=$(c.dataset.copy);copyText(n.textContent,n);return}
  const b=e.target.closest("button[data-act]");if(!b)return;const it=b.closest(".item"),id=it.dataset.id,act=b.dataset.act;
  if(act==="copy"){const n=it.querySelector("pre.draft");copyText(n.textContent,n);return}
  if(DEMO)return toast("예시 데이터라 저장하지 않아요");
  if(act==="posted"){const url=await ask("게시한 댓글 주소","https://…");if(url)mark(id,{status:"posted",posted_url:url});return}
  if(act==="skip"){const why=await ask("제외 사유","예: 홍보 금지 게시판, 질문과 무관");if(why!==null)mark(id,{status:"skip",removal_reason:why});return}
  mark(id,{status:act})});
function ask(label,ph){return new Promise(res=>{const d=document.createElement("div");d.className="scrim";
  d.innerHTML=`<div class="sheet" role="dialog" aria-modal="true" aria-labelledby="askL"><label id="askL" class="t-title b8 ink" for="askIn">${esc(label)}</label>
   <input id="askIn" class="t-body" type="text" placeholder="${esc(ph||"")}"><div class="acts"><button class="btn ghost t-label" id="askNo">취소</button><button class="btn primary t-label" id="askOk">저장</button></div></div>`;
  document.body.appendChild(d);const i=d.querySelector("#askIn");i.focus();const done=v=>{d.remove();res(v)};
  d.querySelector("#askOk").onclick=()=>done(i.value.trim());d.querySelector("#askNo").onclick=()=>done(null);
  d.onclick=e=>{if(e.target===d)done(null)};i.onkeydown=e=>{if(e.key==="Enter")done(i.value.trim());if(e.key==="Escape")done(null)}})}
["#fProv","#fWhere"].forEach(s=>$(s).addEventListener("change",render));$("#fQ").addEventListener("input",render);
load();loadStatus();setInterval(load,10000);setInterval(loadStatus,2000);
</script></body></html>"""


def main() -> None:
    global DEMO
    ap = argparse.ArgumentParser(prog="python -m agent.admin")
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--demo", action="store_true", help="examples/queue.sample.csv를 읽기 전용으로 보여준다")
    a = ap.parse_args()
    DEMO = a.demo
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    print(f"GTM 관리자 http://127.0.0.1:{a.port}  ({'예시·읽기 전용' if DEMO else '큐 ' + queue.PATH})  — Ctrl+C로 종료")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
