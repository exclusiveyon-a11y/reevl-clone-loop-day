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
import time
from urllib.parse import parse_qs, urlparse
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
EVENTS = Path(os.environ.get("GTM_EVENTS", "data/events.jsonl"))
SAMPLE_EVENTS = ROOT / "examples" / "events.sample.jsonl"
DAUM_CALLS = Path(os.environ.get("GTM_DAUM_CALLS", "data/daum_calls.json"))
YT_UNITS = Path(os.environ.get("GTM_YOUTUBE_UNITS", "data/youtube_units.json"))
_T0 = time.time()
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


def _count(path: Path, cap: int) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    return {"today": int(d.get(_today(), 0)), "cap": cap}


def events(after: int) -> dict:
    """라이브 이벤트(pipeline이 data/events.jsonl에 쓴다). 실행마다 파일이 비워지고 seq가 1부터 다시 시작한다.
    run = 첫 줄(run_start)의 시각 — 화면이 이 값이 바뀌면 새 실행으로 보고 처음부터 다시 받는다."""
    if DEMO:   # 예시를 2초에 한 줄씩 반복 재생
        lines = [json.loads(l) for l in SAMPLE_EVENTS.read_text(encoding="utf-8").splitlines() if l.strip()]
        n, emitted = len(lines), int((time.time() - _T0) / 2.0) + 1
        out = []
        for k in range(after + 1, emitted + 1):
            ev = dict(lines[(k - 1) % n]); ev["seq"] = k; out.append(ev)
            if len(out) >= 400:
                break
        return {"events": out, "last": emitted, "run": "demo", "reset": False}
    try:
        raw = EVENTS.read_text(encoding="utf-8").splitlines()
    except Exception:
        return {"events": [], "last": 0, "run": "", "reset": after > 0}
    evs = []
    for l in raw:
        try:
            evs.append(json.loads(l))
        except Exception:
            continue          # 쓰는 중인 마지막 줄
    last = evs[-1].get("seq", 0) if evs else 0
    run = evs[0].get("t", "") if evs and evs[0].get("stage") == "run_start" else ""
    reset = after > last
    start = 0 if reset else after
    return {"events": [e for e in evs if e.get("seq", 0) > start][:400], "last": last, "run": run, "reset": reset}


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
            "sources": {"naver": naver_info(), "daum": _count(DAUM_CALLS, 500), "youtube": _count(YT_UNITS, 3000)},
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
        if path == "/api/events":
            try:
                after = max(0, int((parse_qs(urlparse(self.path).query).get("after") or ["0"])[0]))
            except ValueError:
                after = 0
            return self._json(200, events(after))
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
<script>try{if(localStorage.getItem('gtm-theme')==='light')document.documentElement.dataset.theme='light'}catch(e){}</script>
<style>
/* 리블 디자인 시스템 — frontend/app/globals.css 값 그대로(라이트/다크). 컴포넌트는 토큰만 쓴다. */
/* ★기본 = 다크(사용자 지시). 값은 globals.css 다크 토큰 그대로. 라이트는 상단 토글(data-theme=light). */
:root{
  --app:#0e0f11;--bg:#16171a;--hover:#232529;--chip:#232529;--line:#282a2d;--line-strong:#333640;
  --ink:#edeef1;--ink2:#c7ccd4;--sub:#aab1bc;--dim:#8b939f;
  --brand:#4d9fff;--brand-strong:#63acff;--brand-soft:#16233a;--on-brand:#ffffff;
  --up:#ff5c6c;--up-soft:#2b1a1f;--live:#4d9fff;
  --pos:#2dd4a0;--pos-soft:#122a24;--pos-strong:#34d8a6;--warn:#f0b254;--warn-soft:#2e2413;--warn-strong:#f0b254;
  --radius-sm:6px;--radius-md:8px;--radius-lg:10px;--radius-xl:12px;--radius-2xl:16px;
  --font:'Pretendard Variable',Pretendard,-apple-system,'Apple SD Gothic Neo',system-ui,sans-serif;
  --mono:"SF Mono",ui-monospace,Menlo,Consolas,monospace;
  /* 관제 콘솔 면 — 테마와 무관하게 한 톤 더 깊게 */
  --console:#0b0c10;--c-line:#1b1e25;--c-text:#c9cdd4;--c-dim:#6f7784;--c-time:#5d6572;
  --c-brand:#6cb0ff;--c-match:#b8a5ff;--c-pass:#34d8a6;--c-warn:#f0b254;--c-neg:#ff7b85;--c-gray:#8b93a1;
  color-scheme:dark;
}
:root[data-theme=light]{
  --app:#f7f8fa;--bg:#ffffff;--hover:#f4f5f7;--chip:#f2f4f6;--line:#eff1f3;--line-strong:#e3e6ea;
  --ink:#191f28;--ink2:#4e5968;--sub:#616c7a;--dim:#6d7581;
  --brand:#3182f6;--brand-strong:#2272eb;--brand-soft:#eaf3ff;--on-brand:#ffffff;
  --up:#f04452;--up-soft:#feebec;--live:#3182f6;
  --pos:#12b886;--pos-soft:#e6f7f1;--pos-strong:#0b7a5a;--warn:#e8a33d;--warn-soft:#fbf1e0;--warn-strong:#a15c00;
  color-scheme:light;
}
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
/* ── 관제 콘솔 ───────────────────────────────────────────── */
.wrap.wide{max-width:1360px;gap:16px;padding-top:24px}
.topbar{display:flex;align-items:center;gap:10px 16px;flex-wrap:wrap;background:var(--bg);border-radius:var(--radius-2xl);padding:14px 18px}
.topbar .sep{width:1px;height:20px;background:var(--line-strong)}
.live{display:inline-flex;align-items:center;gap:7px;font-weight:800;letter-spacing:.06em;color:var(--up)}
.live i{width:9px;height:9px;border-radius:50%;background:var(--up);animation:blink 1.2s infinite}
.live.off{color:var(--dim)}.live.off i{background:var(--dim);animation:none}
@keyframes blink{50%{opacity:.25}}
.qchips,.schips{display:flex;gap:6px;flex-wrap:wrap;min-width:0}
.qc{border-radius:999px;padding:4px 10px;background:var(--chip);color:var(--ink2);font-weight:600;white-space:nowrap;max-width:220px;overflow:hidden;text-overflow:ellipsis}
.sc{display:inline-flex;align-items:center;gap:6px;border-radius:var(--radius-md);padding:4px 10px;background:var(--app);color:var(--sub);white-space:nowrap}
.sc b{color:var(--ink);font-weight:700}
.sc .mt{width:34px;height:4px;border-radius:2px;background:var(--line-strong);overflow:hidden}.sc .mt i{display:block;height:100%;background:var(--brand)}
.grow{flex:1 1 auto}
@media (max-width:767px){.topbar .sep,.topbar .grow{display:none}.topbar{padding:14px 16px}}
.cgrid{display:grid;grid-template-columns:minmax(0,3fr) minmax(0,6fr) minmax(0,3fr);grid-template-areas:"rail focus res" "rail term res";gap:16px;align-items:start}
.rail{grid-area:rail}.focus{grid-area:focus}.term{grid-area:term}.res{grid-area:res}
@media (max-width:1100px){.cgrid{grid-template-columns:minmax(0,4fr) minmax(0,8fr);grid-template-areas:"rail focus" "rail term" "res res"}}
@media (max-width:767px){.cgrid{grid-template-columns:minmax(0,1fr);grid-template-areas:"focus" "rail" "term" "res"}}
/* 왼쪽 레일 — 파이프라인 단계 */
.rail{padding:18px 18px 20px}
.stages{display:flex;flex-direction:column}
.stg{display:flex;align-items:center;justify-content:space-between;gap:10px;border-radius:var(--radius-xl);padding:12px 12px;transition:background .25s}
.stg .nm{display:flex;align-items:center;gap:10px;color:var(--sub);font-weight:700}
.stg .nm i{width:10px;height:10px;border-radius:50%;background:var(--line-strong);flex:none;transition:background .25s,box-shadow .25s}
.stg .vv{display:flex;flex-direction:column;align-items:flex-end;gap:2px}
.stg .vv .big{color:var(--ink);font-weight:800}
.stg .vv .dr{color:var(--up);font-weight:600}.stg .vv .dr.zero{color:var(--dim);font-weight:500}
.stg.on{background:var(--brand-soft)}.stg.on .nm{color:var(--brand)}.stg.on .nm i{background:var(--brand);box-shadow:0 0 0 4px color-mix(in srgb,var(--brand) 25%,transparent)}
.conn{position:relative;height:22px;margin-left:16px;width:2px;background:var(--line-strong)}
.conn i{position:absolute;left:-3px;top:-4px;width:8px;height:8px;border-radius:50%;background:var(--brand);animation:flow .6s linear forwards}
.conn i.pass{background:var(--pos)}
@keyframes flow{from{top:-4px;opacity:0}25%{opacity:1}to{top:18px;opacity:0}}
@media (max-width:767px){.stages{flex-direction:row;overflow-x:auto;gap:8px;padding-bottom:4px}.conn{display:none}.stg{flex:none;flex-direction:column;align-items:flex-start;background:var(--app);min-width:120px}.stg .vv{align-items:flex-start}}
/* 가운데 위 — 지금 판단 중 */
.focus{padding:20px 22px;display:flex;flex-direction:column;gap:12px;min-height:250px;transition:box-shadow .3s}
.focus.fl-pass{box-shadow:0 0 0 2px var(--pos),0 0 24px color-mix(in srgb,var(--pos) 30%,transparent)}
.focus.fl-skip{box-shadow:0 0 0 2px var(--up),0 0 24px color-mix(in srgb,var(--up) 25%,transparent)}
.fc-top{display:flex;align-items:center;justify-content:space-between;gap:8px}
.eyebrow{color:var(--brand);letter-spacing:.04em}
.fc-chips{display:flex;gap:6px;flex-wrap:wrap;align-items:center;min-width:0}
.fc-ch{border-radius:999px;padding:4px 10px;background:var(--chip);color:var(--ink);font-weight:700;white-space:nowrap}
.fc-wh{color:var(--sub);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fc-t{margin:0;color:var(--ink);font-size:20px;line-height:1.35;font-weight:800;letter-spacing:-0.02em;word-break:keep-all;overflow-wrap:anywhere;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.fc-steps{display:flex;gap:6px;flex-wrap:wrap}
.st{display:inline-flex;align-items:center;gap:5px;border-radius:999px;padding:5px 11px;background:var(--chip);color:var(--dim);font-weight:700;transition:background .25s,color .25s}
.st .ico{width:13px;height:13px}
.st.act{background:var(--brand-soft);color:var(--brand);animation:stp 1s infinite}
.st.ok{background:var(--brand-soft);color:var(--brand)}
.st.no{background:var(--up-soft);color:var(--up)}
.st.okg{background:var(--pos-soft);color:var(--pos-strong)}
@keyframes stp{50%{opacity:.5}}
.mchips{display:flex;gap:6px;flex-wrap:wrap;min-height:26px}
.mchip{border-radius:var(--radius-sm);padding:4px 9px;background:var(--pos-soft);color:var(--pos-strong);font-weight:700;animation:slideIn .35s ease-out}
.mchip.hub{background:var(--brand-soft);color:var(--brand)}
@keyframes slideIn{from{opacity:0;transform:translateX(-10px)}to{opacity:1;transform:none}}
.fbars{display:grid;grid-template-columns:repeat(4,minmax(0,1fr)) auto;gap:12px;align-items:end}
.fb{display:flex;flex-direction:column;gap:5px;min-width:0}
.fb .tr{height:6px;border-radius:99px;background:var(--chip);overflow:hidden}
.fb .tr i{display:block;height:100%;width:0;background:var(--brand);transition:width .7s cubic-bezier(.2,.8,.2,1)}
.fb.rule .tr i{background:var(--pos)}
.ftot{color:var(--ink);font-weight:800}
.fres{display:flex;align-items:center;gap:8px;min-height:28px}
.skel .fc-t,.skel .fc-ch,.skel .fc-wh,.skel .st{color:transparent!important;background:linear-gradient(90deg,var(--chip) 25%,var(--hover) 50%,var(--chip) 75%)!important;background-size:200% 100%!important;animation:shim 1.1s infinite linear!important;border-radius:var(--radius-sm)}
.skel .fc-t{min-height:54px}
@keyframes shim{from{background-position:200% 0}to{background-position:-200% 0}}
/* 가운데 아래 — 콘솔 로그 */
.term{background:var(--console);border-radius:var(--radius-2xl);overflow:hidden;display:flex;flex-direction:column;min-width:0}
.term-h{display:flex;align-items:center;gap:10px;padding:11px 16px;border-bottom:1px solid var(--c-line);color:var(--c-dim);font-family:var(--mono);font-size:12px}
.term-h .dots{display:flex;gap:6px}.term-h .dots i{width:10px;height:10px;border-radius:50%;background:#2a2e37}
.term-h .tt{color:var(--c-text);font-weight:600}
.term-h button{margin-left:auto;border:0;border-radius:6px;background:#1c2029;color:var(--c-text);padding:3px 9px;font:inherit;cursor:pointer}
.term-b{height:380px;overflow-y:auto;padding:12px 16px 16px;font-family:var(--mono);font-size:12.5px;line-height:1.6;color:var(--c-text);scrollbar-color:#2a2e37 transparent}
@media (max-width:767px){.term-b{height:50vh;font-size:12px;padding:10px 12px}}
.ln{display:grid;grid-template-columns:62px 58px minmax(0,1fr);gap:10px;white-space:nowrap;cursor:pointer;border-radius:4px;padding:1px 6px;margin:0 -6px}
@media (max-width:767px){.ln{grid-template-columns:56px 50px minmax(0,1fr);gap:6px}}
.ln:hover{background:#ffffff0d}
.ln .tm{color:var(--c-time)}.ln .tx{overflow:hidden;text-overflow:ellipsis;color:var(--c-text)}
.ln .tg{font-weight:700}
.tg.k-found{color:var(--c-brand)}.tg.k-check{color:var(--c-dim)}.tg.k-match{color:var(--c-match)}.tg.k-score{color:var(--c-text)}
.tg.k-pass{color:var(--c-pass)}.tg.k-run{color:var(--c-pass)}.tg.k-skipg{color:var(--c-gray)}.tg.k-skipo{color:var(--c-warn)}.tg.k-skipr{color:var(--c-neg)}
.ln.pass .tx{color:var(--c-pass)}
.ln.typ .tx{animation:typ .4s steps(24,end)}
@keyframes typ{from{clip-path:inset(0 100% 0 0)}to{clip-path:inset(0 0 0 0)}}
.term-b:focus-visible{outline:2px solid var(--c-brand);outline-offset:-2px}
/* 오른쪽 — 결과 */
.res{padding:18px 18px 20px;display:flex;flex-direction:column;gap:4px}
.plist{display:flex;flex-direction:column;max-height:330px;overflow-y:auto}
.pr{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:4px 10px;align-items:center;padding:11px 0;border-top:1px solid var(--line);animation:slideIn .35s ease-out}
.pr:first-child{border-top:0}
.pr .pt{color:var(--ink);font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pr .pd{grid-column:1/2;color:var(--sub);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ibtn{grid-row:1/3;grid-column:2;width:36px;height:36px;border-radius:var(--radius-lg);border:0;background:var(--chip);color:var(--ink);display:grid;place-items:center;cursor:pointer}
.ibtn:hover{background:var(--brand-soft);color:var(--brand)}
.rbars{display:flex;flex-direction:column;gap:2px}
.rbar{display:grid;grid-template-columns:118px minmax(0,1fr) 40px;gap:10px;align-items:center;padding:8px 8px;margin:0 -8px;border:0;background:none;color:inherit;font:inherit;text-align:left;cursor:pointer;border-radius:var(--radius-md)}
.rbar:hover,.rbar[aria-pressed=true]{background:var(--hover)}
.rbar .tr{height:8px;border-radius:99px;background:var(--chip);overflow:hidden}
.rbar .tr i{display:block;height:100%;transition:width .5s cubic-bezier(.2,.8,.2,1)}
.rbar .k{color:var(--ink2);font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rbar .v{color:var(--ink);font-weight:800;text-align:right}
.muted{color:var(--dim);padding:14px 0}
/* 검토 대기함 접기 */
.item.hl,.mini.hl{box-shadow:0 0 0 2px var(--brand);border-radius:var(--radius-lg);transition:box-shadow .3s}
.review{display:flex;flex-direction:column;gap:12px}
.themebtn{display:inline-flex;align-items:center;gap:6px;min-height:36px;padding:0 12px;border-radius:var(--radius-xl);border:0;background:var(--chip);color:var(--ink);font-weight:700;cursor:pointer}
@media (prefers-reduced-motion:reduce){.live i,.st.act,.mchip,.pr,.ln.typ .tx,.conn i,.skel .fc-t,.skel .fc-ch,.skel .fc-wh,.skel .st{animation:none!important}.fb .tr i,.rbar .tr i,.focus,.stg{transition:none!important}}
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
<div class="wrap wide">
 <header class="top">
  <div><div class="mark t-eyebrow"><i></i>리블 · 로컬 관리자 127.0.0.1</div><h1 class="t-display">GTM Agent</h1></div>
  <div class="bar"><span class="badge demo t-cap" id="demo" hidden>예시 데이터 · 읽기 전용</span><span class="t-cap dim num" id="at"></span>
   <button type="button" class="themebtn t-label" id="themeBtn" aria-label="라이트·다크 전환">다크</button></div>
 </header>

 <section class="topbar" id="topbar" aria-label="실행 상태">
  <span class="live off t-cap" id="live"><i></i>LIVE</span>
  <span class="t-label ink b7 num" id="clock">대기 중</span>
  <span class="sep"></span>
  <div class="qchips" id="qchips"></div>
  <span class="grow"></span>
  <div class="schips" id="schips"></div>
  <span class="t-label sub num" id="rate"></span>
  <div class="filters" role="group" aria-label="재생 속도">
   <button type="button" class="chip t-cap b7" data-speed="1" aria-pressed="true">1×</button>
   <button type="button" class="chip t-cap b7" data-speed="2" aria-pressed="false">2×</button>
   <button type="button" class="chip t-cap b7" data-speed="0" aria-pressed="false">일시정지</button>
   <button type="button" class="chip t-cap b7" id="replayBtn" hidden>다시 보기</button>
  </div>
 </section>

 <div class="cgrid">
  <aside class="card rail" aria-label="파이프라인 단계">
   <div class="head"><div><h2>파이프라인</h2><p class="t-cap">단계마다 통과 수 · 여기서 빠진 수</p></div></div>
   <div class="stages" id="stages"></div>
  </aside>
  <section class="card focus" id="focus" aria-live="polite"></section>
  <section class="term" aria-label="콘솔 로그">
   <div class="term-h"><span class="dots"><i></i><i></i><i></i></span><span class="tt">gtm-agent · live</span><span id="termHint">마우스를 올리면 멈춰요</span><button type="button" id="termClear" hidden>필터 해제</button></div>
   <div class="term-b" id="termB" tabindex="0"></div>
  </section>
  <aside class="card res" aria-label="결과">
   <div class="head"><div><h2>통과</h2><p class="t-cap">사람 확인·게시 대기 · 최근 순</p></div><span class="t-val-xl b8 ink num" id="passN">0</span></div>
   <div class="plist" id="plist"><div class="muted t-cap">아직 통과한 글이 없어요</div></div>
   <div class="head" style="margin-top:18px"><div><h2>제외 사유</h2><p class="t-cap">누르면 로그를 그 사유만</p></div><span class="t-val-xl b8 ink num" id="skipN">0</span></div>
   <div class="rbars" id="rbars"><div class="muted t-cap">제외된 글이 없어요</div></div>
  </aside>
 </div>

 <section class="review">
  <button type="button" class="fold t-label" id="revBtn" aria-expanded="false">검토 대기함 펼치기</button>
  <div class="review" id="revBody" hidden>
 <section class="card run" id="run" hidden></section>
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
 </section>
</div>
<div class="toast t-label" id="toast" role="status"></div>
<script>
const TOKEN="__TOKEN__", DEMO=__DEMO__;
const ST={needs_human:"사람 확인",ready:"게시 대기",posted:"게시됨",skip:"제외",removed:"삭제·신고"};
const PROV={naver_cafe:"네이버 카페",naver_kin:"지식iN",naver_blog:"네이버 블로그",daum_cafe:"다음 카페",daum_web:"다음 웹",ppomppu:"뽐뿌",ppomppu_web:"뽐뿌",clien:"클리앙",dcinside:"디시",fmkorea:"에펨코리아",theqoo:"더쿠",blind:"블라인드",mlbpark:"엠팍",bobaedream:"보배드림",ruliweb:"루리웹",daangn:"당근",youtube:"유튜브",community:"커뮤니티",copy:"복사",web_share:"공유"};
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
  const sp=e.target.closest("[data-speed]");if(sp){SPEED=Number(sp.dataset.speed);document.querySelectorAll("[data-speed]").forEach(b=>b.setAttribute("aria-pressed",String(b===sp)));if(SPEED)pump();return}
  if(e.target.closest("#replayBtn")){const all=EV_ALL.slice();resetLive();PQ=all;if(!SPEED){SPEED=1;document.querySelectorAll("[data-speed]").forEach(b=>b.setAttribute("aria-pressed",String(b.dataset.speed==="1")))}pump();return}
  if(e.target.closest("#themeBtn")){setTheme(document.documentElement.dataset.theme==="light"?"dark":"light");return}
  if(e.target.closest("#revBtn")){REV=!REV;$("#revBody").hidden=!REV;const b=$("#revBtn");b.setAttribute("aria-expanded",String(REV));b.textContent=REV?"검토 대기함 접기":"검토 대기함 펼치기";return}
  const pc=e.target.closest("[data-pcopy]");if(pc){const t=DRAFT[pc.dataset.pcopy]||"";const n=document.createElement("pre");n.textContent=t;copyText(t,n);return}
  const rb=e.target.closest(".rbar");if(rb){TF=TF===rb.dataset.reason?"":rb.dataset.reason;document.querySelectorAll("#termB .ln").forEach(l=>l.hidden=!!TF&&l.dataset.reason!==TF);$("#termClear").hidden=!TF;renderReasons();return}
  if(e.target.closest("#termClear")){TF="";document.querySelectorAll("#termB .ln").forEach(l=>l.hidden=false);$("#termClear").hidden=true;renderReasons();return}
  const ln=e.target.closest(".ln[data-id],.pr[data-id]");if(ln){jumpTo(ln.dataset.id);return}
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

/* ── 라이브 관제: events.jsonl을 재생 큐로 한 단계씩 보여준다 ───────────────── */
const RM=matchMedia("(prefers-reduced-motion: reduce)").matches;
const STG=[["search","검색"],["q","질문 판정"],["m","매칭"],["d","초안"],["s","점수"],["c","분류"]];
const STEP=[["q","질문 판정"],["m","매칭"],["d","초안"],["s","점수"],["r","결과"]];
const RTONE={not_question:"g",same_cafe_today:"g",no_match:"o",error:"o",low_score:"r",same_draft:"r"};
const RCOL={g:"var(--dim)",o:"var(--warn)",r:"var(--up)"};
let EV_LAST=0,EV_ALL=[],PQ=[],SPEED=1,PUMPING=false,RUNKEY=null,FIRST=true,HOLD=false,TF="",REV=false;
let CNT,RC,PASS,DRAFT,URLS,RUN,DONE_T,FOC;
function resetLive(){CNT={search:0,q:0,qd:0,m:0,md:0,d:0,s:0,sd:0,c:0,cd:0};RC={};PASS=[];DRAFT={};URLS={};
  RUN={t0:"",tl:"",q:[],total:0,i:0,ended:false,sum:null};DONE_T=[];FOC=null;$("#termB").innerHTML="";renderStages();renderPass();renderReasons();renderFocus(null);renderTop()}
const sec=t=>{const m=/(\d+):(\d+):(\d+)/.exec(t||"");return m?(+m[1])*3600+(+m[2])*60+(+m[3]):0};
const mmss=n=>`${String(Math.floor(n/60)).padStart(2,"0")}:${String(n%60).padStart(2,"0")}`;
function renderTop(){const live=!!(DEMO||(STATUS&&STATUS.run&&STATUS.run.running)||PQ.length)&&!RUN.ended;
  $("#live").classList.toggle("off",!live);
  const el=RUN.t0?Math.max(0,sec(RUN.tl)-sec(RUN.t0)):0;
  $("#clock").textContent=RUN.t0?`${RUN.t0.slice(0,5)} 시작 · 경과 ${mmss(el)}${RUN.total?` · ${fmt(RUN.i)}/${fmt(RUN.total)}`:""}`:"대기 중";
  $("#qchips").innerHTML=(RUN.q||[]).slice(0,5).map(q=>`<span class="qc t-cap">#${esc(q)}</span>`).join("");
  const so=(STATUS&&STATUS.sources)||{};
  $("#schips").innerHTML=[["네이버",so.naver,"회"],["다음",so.daum,"회"],["유튜브",so.youtube,"단위"]].filter(x=>x[1]).map(([n,v,u])=>{const pc=v.cap?Math.min(100,v.today/v.cap*100):0;
    return `<span class="sc t-cap" title="오늘 ${fmt(v.today)}/${fmt(v.cap)}${u}">${n} <b class="num">${fmt(v.today)}</b><span class="num">/${fmt(v.cap)}</span><span class="mt"><i style="width:${pc}%"></i></span></span>`}).join("");
  const now=performance.now();DONE_T=DONE_T.filter(t=>now-t<10000);
  $("#rate").textContent=DONE_T.length?`${(DONE_T.length/10).toFixed(1)}건/초`:"";
  $("#replayBtn").hidden=!(RUN.ended&&EV_ALL.length);
}
function renderStages(){const v={search:[CNT.search,0],q:[CNT.q,CNT.qd],m:[CNT.m,CNT.md],d:[CNT.d,0],s:[CNT.s,CNT.sd],c:[CNT.c,CNT.cd]};
  $("#stages").innerHTML=STG.map(([k,l],i)=>`${i?`<div class="conn" id="cn-${k}"></div>`:""}<div class="stg" id="sg-${k}"><span class="nm t-label"><i></i>${l}</span>
   <span class="vv"><span class="big t-val-xl num">${fmt(v[k][0])}</span>${k==="search"?`<span class="dr zero t-micro">찾음</span>`:`<span class="dr ${v[k][1]?"":"zero"} t-micro num">${v[k][1]?"−"+fmt(v[k][1])+" 빠짐":"빠짐 0"}</span>`}</span></div>`).join("");
  setActive(FOC&&FOC.stage)}
function setActive(k){document.querySelectorAll(".stg").forEach(e=>e.classList.toggle("on",e.id==="sg-"+k))}
function flowDot(k,cls){if(RM)return;const c=$("#cn-"+k);if(!c)return;const d=document.createElement("i");if(cls)d.className=cls;c.appendChild(d);setTimeout(()=>d.remove(),700)}
function stepHtml(st){return STEP.map(([k,l])=>{const v=st[k]||"";return `<span class="st t-cap ${v}">${v==="ok"||v==="okg"?ic("check"):v==="no"?ic("x"):""}${l}</span>`}).join("")}
function renderFocus(f,skel){const el=$("#focus");
  if(!f){el.className="card focus skel";el.innerHTML=`<div class="fc-top"><span class="eyebrow t-eyebrow b7">지금 판단 중</span><span class="t-cap dim">다음 글을 기다리는 중</span></div>
    <div class="fc-chips"><span class="fc-ch t-cap">채널</span><span class="fc-wh t-cap">카페 이름</span></div><p class="fc-t">제목을 불러오는 중</p><div class="fc-steps">${stepHtml({})}</div>`;return}
  el.className="card focus"+(skel?" skel":"")+(f.flash?" fl-"+f.flash:"");
  const b=f.bars||null, tot=b?b.total:"";
  el.innerHTML=`<div class="fc-top"><span class="eyebrow t-eyebrow b7">지금 판단 중</span><span class="t-cap dim num">${f.i?`${fmt(f.i)} / ${fmt(f.total)}`:""}</span></div>
   <div class="fc-chips"><span class="fc-ch t-cap">${esc(PROV[f.channel]||PROV[f.provider]||f.channel||"-")}</span><span class="fc-wh t-cap">${esc(f.where||"")}</span>${f.query?`<span class="qc t-cap">#${esc(f.query)}</span>`:""}</div>
   <p class="fc-t">${esc(f.title||"(제목 없음)")}</p>
   <div class="fc-steps">${stepHtml(f.st)}</div>
   <div class="mchips">${(f.chips||[]).map(c=>`<span class="mchip t-cap ${c.hub?"hub":""}">${esc(c.n)}</span>`).join("")}</div>
   ${b?`<div class="fbars">${[["적합","fit",3],["데이터","data",3],["최신","fresh",2],["규칙","rule",2]].map(([l,k,mx])=>`<div class="fb ${k}"><span class="t-micro dim">${l} <b class="ink num">${b[k]}</b></span><span class="tr"><i data-w="${Math.round((b[k]||0)/mx*100)}"></i></span></div>`).join("")}<span class="ftot t-val num">${esc(tot)}점</span></div>`:""}
   <div class="fres t-label">${f.res||""}</div>`;
  const bars=el.querySelectorAll(".fb .tr i");if(bars.length){if(RM||!f.anim)bars.forEach(i=>i.style.width=i.dataset.w+"%");else requestAnimationFrame(()=>requestAnimationFrame(()=>bars.forEach(i=>i.style.width=i.dataset.w+"%")))}}
function log(tag,cls,txt,ev,anim,extra){const b=$("#termB");const d=document.createElement("div");d.className="ln"+(anim&&!RM?" typ":"")+(cls==="pass"?" pass":"");
  if(ev&&ev.id)d.dataset.id=ev.id;if(extra&&extra.reason)d.dataset.reason=extra.reason;
  d.innerHTML=`<span class="tm num">${esc((ev&&ev.t)||"")}</span><span class="tg k-${cls}">${tag}</span><span class="tx">${txt}</span>`;
  if(TF&&d.dataset.reason!==TF)d.hidden=true;b.appendChild(d);while(b.children.length>600)b.firstChild.remove();if(!HOLD)b.scrollTop=b.scrollHeight}
function renderPass(){$("#passN").textContent=fmt(PASS.length);
  $("#plist").innerHTML=PASS.length?PASS.slice(0,40).map(x=>`<div class="pr" data-id="${esc(x.id)}"><span class="pt t-label">${esc(x.title||"(제목 없음)")}</span>
   <span class="pd t-cap">${esc(PROV[x.channel]||x.channel||"")} · ${esc((x.draft||"").split("\n")[0])}</span>
   <button type="button" class="ibtn" data-pcopy="${esc(x.id)}" aria-label="초안 복사" title="초안 복사">${ic("copy")}</button></div>`).join(""):`<div class="muted t-cap">아직 통과한 글이 없어요</div>`}
function renderReasons(){const ent=Object.entries(RC).sort((a,b)=>b[1]-a[1]),n=ent.reduce((a,[,v])=>a+v,0),mx=Math.max(1,...ent.map(x=>x[1]));$("#skipN").textContent=fmt(n);
  $("#rbars").innerHTML=ent.length?ent.map(([k,v])=>`<button type="button" class="rbar" data-reason="${esc(k)}" aria-pressed="${TF===k}"><span class="k t-cap">${esc(WHY[k]||k)}</span><span class="tr"><i style="width:${v/mx*100}%;background:${RCOL[RTONE[k]||"g"]}"></i></span><span class="v t-label num">${fmt(v)}</span></button>`).join(""):`<div class="muted t-cap">제외된 글이 없어요</div>`}
function excerpt(t,n=34){t=String(t||"");return t.length>n?t.slice(0,n)+"…":t}
function apply(ev,anim){
  RUN.tl=ev.t||RUN.tl;
  switch(ev.stage){
   case "run_start":resetLive();RUN.t0=ev.t||"";RUN.tl=ev.t||"";RUN.q=ev.queries||[];
     log("RUN","run",`검색어 ${(ev.queries||[]).length}개 · 소스 ${(ev.providers||[]).length||"기본"}`,ev,anim);break;
   case "searched":RUN.total=ev.total||0;log("SEARCH","found",`${fmt(ev.total)}건 찾음 — 하나씩 판단 시작`,ev,anim);break;
   case "found":CNT.search++;URLS[ev.id]=ev.url||"";RUN.i=ev.i||RUN.i+1;RUN.total=ev.total||RUN.total;
     FOC={id:ev.id,stage:"q",channel:ev.channel,provider:ev.provider,where:ev.where,title:ev.title,query:ev.query,i:ev.i,total:ev.total,st:{q:"act"},chips:[],bars:null,res:"",anim};
     if(anim&&!RM){renderFocus(FOC,true);setTimeout(()=>{if(FOC&&FOC.id===ev.id)renderFocus(FOC)},Math.max(120,220/(SPEED||1)))}else renderFocus(FOC);
     log("FOUND","found",`${esc(PROV[ev.channel]||ev.channel||"")}${ev.where?" / "+esc(ev.where):""}   "${esc(excerpt(ev.title))}"`,ev,anim);flowDot("q");break;
   case "question":CNT.q++;if(FOC&&FOC.id===ev.id){FOC.st.q="ok";FOC.st.m="act";FOC.stage="m";renderFocus(FOC)}
     log("CHECK","check","질문 글 맞음 → 매칭",ev,anim);flowDot("m");break;
   case "match":CNT.m++;if(FOC&&FOC.id===ev.id){FOC.st.m="ok";FOC.st.d="act";FOC.stage="d";
       FOC.chips=(ev.complexes||[]).map(n=>({n})).concat((ev.hubs||[]).map(n=>({n,hub:1})));renderFocus(FOC)}
     log("MATCH","match",`${esc([...(ev.complexes||[]),...(ev.hubs||[])].join(" · ")||"-")} (${esc(ev.kind||"")})`,ev,anim);flowDot("d");break;
   case "score":CNT.d++;if(FOC&&FOC.id===ev.id){FOC.st.d="ok";FOC.st.s="act";FOC.stage="s";FOC.bars={fit:ev.fit,data:ev.data,fresh:ev.fresh,rule:ev.rule,total:ev.total};FOC.anim=anim;renderFocus(FOC)}
     log("SCORE","score",`적합${ev.fit} 데이터${ev.data} 최신${ev.fresh} 규칙${ev.rule} = ${ev.total}`,ev,anim);flowDot("s");break;
   case "result":{const pass=["needs_human","ready","posted"].includes(ev.status);
     if(pass){CNT.s++;CNT.c++;DRAFT[ev.id]=ev.draft||"";PASS.unshift({id:ev.id,title:FOC&&FOC.id===ev.id?FOC.title:"",channel:FOC&&FOC.channel,draft:ev.draft||""});renderPass();flowDot("c","pass");
       log("PASS","pass",`→ ${esc(ST[ev.status]||ev.status)}  ${ev.score!==""?esc(ev.score)+"점":""}`,ev,anim);
       if(FOC&&FOC.id===ev.id){Object.assign(FOC.st,{s:"ok",r:"okg"});FOC.stage="c";FOC.flash="pass";FOC.res=`<span class="badge s-${esc(ev.status)} t-cap">${esc(ST[ev.status]||ev.status)}</span><span class="sub t-cap">${esc(ev.why||"")}</span>`}}
     else{const r=ev.reason||"error",tone=RTONE[r]||"g";RC[r]=(RC[r]||0)+1;
       if(r==="not_question"||r==="same_cafe_today")CNT.qd++;else if(r==="no_match"||r==="error")CNT.md++;else if(r==="low_score")CNT.sd++;else{CNT.s++;CNT.cd++}
       log("SKIP","skip"+tone,esc(WHY[r]||r)+(ev.why&&r!=="not_question"?`  · ${esc(excerpt(ev.why,40))}`:""),ev,anim,{reason:r});renderReasons();
       if(FOC&&FOC.id===ev.id){const fk=r==="not_question"||r==="same_cafe_today"?"q":r==="no_match"||r==="error"?"m":r==="low_score"?"s":"r";
         Object.keys(FOC.st).forEach(k=>{if(FOC.st[k]==="act")delete FOC.st[k]});FOC.st[fk]="no";FOC.st.r="no";FOC.flash="skip";
         FOC.res=`<span class="badge s-skip t-cap">제외</span><span class="t-cap" style="color:${RCOL[tone]}">${esc(WHY[r]||r)}</span>`}}
     if(FOC&&FOC.id===ev.id){FOC.anim=false;renderFocus(FOC)}
     if(anim)DONE_T.push(performance.now());break}
   case "run_end":RUN.ended=true;RUN.sum=ev.summary||{};const sm=RUN.sum;
     log("DONE","run",`발견 ${fmt(sm.found)} · 통과 ${fmt((sm.needs_human||0)+(sm.ready||0)+(sm.posted||0))} · 제외 ${fmt(sm.skip)}`,ev,anim);if(FOC)FOC.stage="";break;
  }
  renderStages();renderTop();
}
const DELAY={found:420,question:300,match:420,score:480,result:620};
function pump(){if(PUMPING)return;PUMPING=true;
  const step=()=>{if(SPEED===0){PUMPING=false;return}
    let ev=PQ.shift();if(!ev){PUMPING=false;renderTop();return}
    if(RM){while(ev){apply(ev,false);ev=PQ.shift()}PUMPING=false;return}
    apply(ev,true);const boost=PQ.length>30?Math.min(10,PQ.length/30):1;
    setTimeout(step,(DELAY[ev.stage]||0)/SPEED/boost)};step()}
async function pollEvents(){let wait=5000;
  try{const r=await fetch(`/api/events?after=${EV_LAST}`,{cache:"no-store"});const j=await r.json();
    if(j.reset||(RUNKEY!==null&&j.run&&j.run!==RUNKEY)){EV_LAST=0;EV_ALL=[];PQ=[];RUNKEY=j.run||"";return pollEvents()}
    RUNKEY=j.run||RUNKEY||"";const evs=j.events||[];
    if(evs.length){EV_LAST=evs[evs.length-1].seq;EV_ALL.push(...evs);
      const running=DEMO||(STATUS&&STATUS.run&&STATUS.run.running);
      if(FIRST&&!running){evs.forEach(e=>apply(e,false))}else{PQ.push(...evs);pump()}
      if(evs.length>=400)return pollEvents()}
    FIRST=false;wait=(DEMO||(STATUS&&STATUS.run&&STATUS.run.running)||PQ.length)?1000:5000}
  catch(e){}finally{renderTop();clearTimeout(pollEvents._t);pollEvents._t=setTimeout(pollEvents,wait)}}
function jumpTo(id){const url=URLS[id];REV=true;$("#revBody").hidden=false;$("#revBtn").setAttribute("aria-expanded","true");$("#revBtn").textContent="검토 대기함 접기";
  let hit=url&&[...document.querySelectorAll("#list a.title")].find(a=>a.getAttribute("href")===url);
  if(!hit&&url){SHOW_SKIP=true;render();hit=[...document.querySelectorAll("#skipWrap a")].find(a=>a.getAttribute("href")===url)}
  const box=hit&&(hit.closest(".item")||hit.closest(".mini"));
  if(box){box.scrollIntoView({behavior:RM?"auto":"smooth",block:"center"});box.classList.add("hl");setTimeout(()=>box.classList.remove("hl"),1800)}else toast("검토 대기함에 아직 없어요 — 10초 안에 갱신돼요")}
function setTheme(t){const root=document.documentElement;if(t==="light")root.dataset.theme="light";else delete root.dataset.theme;
  $("#themeBtn").textContent=t==="light"?"라이트":"다크";try{localStorage.setItem("gtm-theme",t)}catch(e){}}

$("#termB").addEventListener("mouseenter",()=>{HOLD=true;$("#termHint").textContent="멈춤 — 마우스를 치우면 이어서"});
$("#termB").addEventListener("mouseleave",()=>{HOLD=false;$("#termHint").textContent="마우스를 올리면 멈춰요";const b=$("#termB");b.scrollTop=b.scrollHeight});
$("#themeBtn").textContent=document.documentElement.dataset.theme==="light"?"라이트":"다크";
resetLive();load();loadStatus();setInterval(load,10000);setInterval(loadStatus,2000);setTimeout(pollEvents,300);setInterval(renderTop,1000);
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
