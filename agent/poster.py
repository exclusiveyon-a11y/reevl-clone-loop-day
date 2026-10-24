"""게시 — Aside 브라우저(이미 로그인된 운영자 계정)로 네이버 카페 댓글·지식iN 답변·블로그 댓글을 단다. 실패하면 사람에게 넘긴다.

- 키·쿠키를 이 저장소가 갖지 않는다. 로그인은 Aside 앱의 운영자 세션이다.
- dry=True면 댓글 칸·등록 버튼을 **찾기만** 하고 입력·등록하지 않는다.
- 등록 뒤 화면에 우리 문장이 보이는지 되읽어 확인한다. 안 보이면 실패로 기록한다(중복 게시 방지 위해 재시도 안 함).
- 채널마다 입력 칸이 다르다(2026-10-24 Aside 실측):
  naver_cafe  cafe_main iframe의 textarea.comment_inbox_text → a.btn_register
  naver_kin   질문 본문의 button._answerWriteButton(상단 메뉴 '답변하기'는 목록으로 가는 링크) → 스마트에디터 → button._answerRegisterButton
  naver_blog  모바일 댓글 페이지(m.blog.naver.com/CommentList.naver)의 .u_cbox_text(contenteditable) → .u_cbox_btn_upload
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

EXE = shutil.which("aside") or str(Path.home() / ".local/bin/aside")


def _js(url: str, text: str, dry: bool) -> str:
    """네이버 카페 댓글."""
    return f"""
const URL = {json.dumps(url)}; const TEXT = {json.dumps(text, ensure_ascii=False)}; const DRY = {str(dry).lower()};
const R = (o) => console.log('RESULT ' + JSON.stringify(o));
const p = await openTab(URL);
await sleep(5000);
// ★카페 본문은 cafe_main iframe 안이다 — 그 프레임을 먼저 본다. count()에 .catch를 잇지 않는다(Aside 런타임 미지원).
const all = [...p.frames(), p.mainFrame()];
const frames = [...all.filter(x => x.name() === 'cafe_main'), ...all.filter(x => x.name() !== 'cafe_main')];
let f = null, box = null;
for (const fr of frames) {{
  let n = 0;
  try {{ n = await fr.locator('textarea.comment_inbox_text, textarea[placeholder*="댓글"]').count(); }} catch (e) {{ n = 0; }}
  let nb = 0;
  try {{ nb = await fr.locator('a.btn_register, button.btn_register').count(); }} catch (e) {{ nb = 0; }}
  if (n && nb) {{ f = fr; box = fr.locator('textarea.comment_inbox_text, textarea[placeholder*="댓글"]').first(); break; }}
}}
if (!box) {{ R({{ok:false, why:'댓글 칸 없음 — 가입·등급 제한이거나 로그인이 풀렸다'}}); }}
else {{
  const btn = f.locator('a.btn_register, button.btn_register').first();
  if (!(await btn.count())) {{ R({{ok:false, why:'등록 버튼 없음'}}); }}
  else if (DRY) {{ R({{ok:true, mode:'dry', url:p.url()}}); }}
  else {{
    await box.click(); await sleep(400);
    for (const [i, line] of TEXT.split('\\n').entries()) {{
      if (i) await p.keyboard.press('Shift+Enter');
      if (line) await p.keyboard.type(line, {{delay: 5}});
    }}
    await sleep(800);
    let typed = ''; try {{ typed = await box.inputValue(); }} catch (e) {{}}
    if (!typed.includes(TEXT.slice(0, 12))) {{ R({{ok:false, why:'입력이 안 들어갔다'}}); }}
    else {{
      await btn.click(); await sleep(3500);
      let body = ''; try {{ body = await f.locator('body').innerText(); }} catch (e) {{}}
      R(body.includes(TEXT.slice(0, 20)) ? {{ok:true, mode:'posted', url:p.url()}}
                                        : {{ok:false, why:'등록 후 댓글이 안 보인다 — 사람 확인 필요'}});
    }}
  }}
}}
"""


_HEAD = """
const URL = {url}; const TEXT = {text}; const DRY = {dry};
const R = (o) => console.log('RESULT ' + JSON.stringify(o));
const p = await openTab(URL);
await sleep(5000);
"""


def _head(url: str, text: str, dry: bool) -> str:
    return _HEAD.format(url=json.dumps(url), text=json.dumps(text, ensure_ascii=False), dry=str(dry).lower())


def _js_kin(url: str, text: str, dry: bool) -> str:
    """지식iN 답변 — 스마트에디터. 등록 확인 대화상자가 뜨면 메시지를 남기고 수락한다(사람이 승인한 게시만 여기 온다)."""
    return _head(url, text, dry) + """
const dialogs = [];
p.on('dialog', async d => { dialogs.push(d.message().slice(0, 80)); try { await d.accept(); } catch (e) {} });
const open = p.locator('button._answerWriteButton');
let n = 0; try { n = await open.count(); } catch (e) { n = 0; }
if (!n) { R({ok:false, why:'답변 버튼 없음 — 마감·삭제된 질문이거나 로그인이 풀렸다'}); }
else {
  await open.first().click(); await sleep(4000);
  const reg = p.locator('button._answerRegisterButton');
  const area = p.locator('.se-placeholder, .se-text-paragraph');
  let nr = 0, na = 0;
  try { nr = await reg.count(); } catch (e) {}
  try { na = await area.count(); } catch (e) {}
  if (!nr || !na) { R({ok:false, why:'답변 편집기 없음'}); }
  else if (DRY) { R({ok:true, mode:'dry', url:p.url()}); }
  else {
    await area.first().click(); await sleep(500);
    for (const [i, line] of TEXT.split('\\n').entries()) {
      if (i) await p.keyboard.press('Enter');
      if (line) await p.keyboard.type(line, {delay: 5});
    }
    await sleep(1000);
    let typed = ''; try { typed = await p.locator('.se-content, .se-main-container').first().innerText(); } catch (e) {}
    if (!typed.includes(TEXT.slice(0, 12))) { R({ok:false, why:'입력이 안 들어갔다', dialogs}); }
    else {
      await reg.first().click(); await sleep(5000);
      await p.reload(); await sleep(5000);
      let body = ''; try { body = await p.locator('body').innerText(); } catch (e) {}
      R(body.includes(TEXT.slice(0, 20)) ? {ok:true, mode:'posted', url:p.url(), dialogs}
                                        : {ok:false, why:'등록 후 답변이 안 보인다 — 사람 확인 필요', dialogs});
    }
  }
}
"""


def _blog_mobile(url: str) -> str:
    """blog.naver.com/{id}/{logNo} → 모바일 댓글 주소. PC 화면은 '댓글 쓰기'를 눌러도 Aside에서 댓글창이 안 열렸다(10-24 실측)."""
    m = re.search(r"blog\.naver\.com/([^/?#]+)/(\d+)", url) or \
        re.search(r"blogId=([^&]+).*?logNo=(\d+)", url)
    return f"https://m.blog.naver.com/CommentList.naver?blogId={m.group(1)}&logNo={m.group(2)}" if m else url


def _js_blog(url: str, text: str, dry: bool) -> str:
    """네이버 블로그 댓글 — 모바일 댓글 페이지의 .u_cbox_text(contenteditable) → .u_cbox_btn_upload."""
    return _head(_blog_mobile(url), text, dry) + """
const box = p.locator('.u_cbox_text[contenteditable=true], .u_cbox_text');
const btn = p.locator('.u_cbox_btn_upload');
let nb = 0, nu = 0;
try { nb = await box.count(); } catch (e) {}
try { nu = await btn.count(); } catch (e) {}
if (!nb || !nu) { R({ok:false, why:'댓글 칸 없음 — 댓글을 막았거나 이웃만 허용, 또는 로그인이 풀렸다'}); }
else if (DRY) { R({ok:true, mode:'dry', url:p.url()}); }
else {
  await box.first().click(); await sleep(400);
  for (const [i, line] of TEXT.split('\\n').entries()) {
    if (i) await p.keyboard.press('Shift+Enter');
    if (line) await p.keyboard.type(line, {delay: 5});
  }
  await sleep(800);
  let typed = ''; try { typed = await box.first().innerText(); } catch (e) {}
  if (!typed.includes(TEXT.slice(0, 12))) { R({ok:false, why:'입력이 안 들어갔다'}); }
  else {
    await btn.first().click(); await sleep(3500);
    let body = ''; try { body = await p.locator('body').innerText(); } catch (e) {}
    R(body.includes(TEXT.slice(0, 20)) ? {ok:true, mode:'posted', url:p.url()}
                                      : {ok:false, why:'등록 후 댓글이 안 보인다 — 사람 확인 필요'});
  }
}
"""


SCRIPTS = {"naver_cafe": _js, "naver_kin": _js_kin, "naver_blog": _js_blog}


def post(channel: str, url: str, text: str, dry: bool = True, timeout: int = 180) -> dict:
    # ★자동 게시 중지(2026-10-24 사용자 지시 "자동 게시는 중지시켜두고 탐색, 맞춤 댓글까지").
    #   GTM_AUTOPUBLISH=1이 아니면 실제 등록은 하지 않는다 — --publish를 줘도 사람 대기열로 간다.
    if not dry and os.environ.get("GTM_AUTOPUBLISH") != "1":
        return {"ok": False, "why": "자동 게시 중지됨(GTM_AUTOPUBLISH≠1) — 사람 게시", "human": True}
    if channel not in SCRIPTS:
        return {"ok": False, "why": f"{channel} 자동 게시 미지원 — 사람 게시", "human": True}
    try:
        r = subprocess.run([EXE, "repl", SCRIPTS[channel](url, text, dry)], capture_output=True, text=True, timeout=timeout)
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
