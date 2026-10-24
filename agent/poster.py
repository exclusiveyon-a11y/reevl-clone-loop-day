"""게시 — Aside 브라우저(이미 로그인된 운영자 계정)로 네이버 카페 댓글을 단다. 실패하면 사람에게 넘긴다.

- 키·쿠키를 이 저장소가 갖지 않는다. 로그인은 Aside 앱의 운영자 세션이다.
- dry=True면 댓글 칸·등록 버튼을 **찾기만** 하고 입력·등록하지 않는다.
- 등록 뒤 화면에 우리 문장이 보이는지 되읽어 확인한다. 안 보이면 실패로 기록한다(중복 게시 방지 위해 재시도 안 함).
- 지식iN 답변은 편집기 구조가 달라 v0에서는 사람 게시로 넘긴다.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

EXE = shutil.which("aside") or str(Path.home() / ".local/bin/aside")


def _js(url: str, text: str, dry: bool) -> str:
    return f"""
const URL = {json.dumps(url)}; const TEXT = {json.dumps(text, ensure_ascii=False)}; const DRY = {str(dry).lower()};
const R = (o) => console.log('RESULT ' + JSON.stringify(o));
const p = await openTab(URL);
await sleep(5000);
const frames = [p.mainFrame(), ...p.frames()];
let f = null, box = null;
for (const fr of frames) {{
  const b = fr.locator('textarea.comment_inbox_text, textarea[placeholder*="댓글"]');
  if (await b.count().catch(() => 0)) {{ f = fr; box = b.first(); break; }}
}}
if (!box) {{ R({{ok:false, why:'댓글 칸 없음 — 가입·등급 제한이거나 로그인이 풀렸다'}}); }}
else {{
  const btn = f.locator('a.btn_register, button.btn_register, a:has-text("등록"), button:has-text("등록")').first();
  if (!(await btn.count())) {{ R({{ok:false, why:'등록 버튼 없음'}}); }}
  else if (DRY) {{ R({{ok:true, mode:'dry', url:p.url()}}); }}
  else {{
    await box.click(); await sleep(400);
    for (const [i, line] of TEXT.split('\\n').entries()) {{
      if (i) await p.keyboard.press('Shift+Enter');
      if (line) await p.keyboard.type(line, {{delay: 5}});
    }}
    await sleep(800);
    const typed = await box.inputValue().catch(() => '');
    if (!typed.includes(TEXT.slice(0, 12))) {{ R({{ok:false, why:'입력이 안 들어갔다'}}); }}
    else {{
      await btn.click(); await sleep(3500);
      const body = await f.locator('body').innerText().catch(() => '');
      R(body.includes(TEXT.slice(0, 20)) ? {{ok:true, mode:'posted', url:p.url()}}
                                        : {{ok:false, why:'등록 후 댓글이 안 보인다 — 사람 확인 필요'}});
    }}
  }}
}}
"""


def post(channel: str, url: str, text: str, dry: bool = True, timeout: int = 180) -> dict:
    if channel != "naver_cafe":
        return {"ok": False, "why": f"{channel} 자동 게시는 v0 미지원 — 사람 게시", "human": True}
    try:
        r = subprocess.run([EXE, "repl", _js(url, text, dry)], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return {"ok": False, "why": "aside CLI 없음 — Aside.app 실행 확인", "human": True}
    except subprocess.TimeoutExpired:
        return {"ok": False, "why": f"{timeout}초 초과", "human": True}
    out = (r.stdout or "") + (r.stderr or "")
    for ln in reversed(out.splitlines()):
        if ln.startswith("RESULT "):
            res = json.loads(ln[7:])
            res.setdefault("human", not res.get("ok"))
            return res
    return {"ok": False, "why": "Aside 결과 없음(데몬·로그인 확인)", "tail": out[-200:], "human": True}
