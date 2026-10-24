"""글쓴이가 실제로 궁금해하는 것(페인포인트)을 잡는다 — 규칙 기반, 유료 LLM 없음.

각 의도마다 ①질문을 짚는 한 줄 ②확인된 데이터로 답 ③리블에서 그 고민을 푸는 기능 한 줄을 정한다.
★리블에 실제로 있는 기능만 말한다(비교표·단지 실거래 이력·전세가율·학교/역 주변 단지 목록).
★AI 전망·순위·'오른다/내린다' 같은 단정은 쓰지 않는다. 학교 배정은 확정이라고 하지 않는다.
"""
from __future__ import annotations

import re

# (이름, 신호 정규식) — 앞에 있을수록 우선
RULES = [
    ("compare", r"vs|VS|비교|어디가|어느\s*(쪽|곳|단지)|중에|고민|둘\s*중|갈아타|vs\.?"),
    ("school", r"배정|학군|초등|중학교|고등학교|초품아|학원가|\w+초\b|\w+중\b"),
    ("price", r"시세|얼마|비싼|싼가|고점|떨어|빠졌|올랐|오를|적정|호가|실거래|매수\s*타이밍|살까|팔까"),
    ("jeonse", r"전세|전세가율|갭|월세|보증금|역전세"),
    ("rebuild", r"재건축|리모델링|연식|구축|용적률|대지지분"),
    ("commute", r"출퇴근|통근|역세권|지하철|노선|역\s*근처|강남까지|광화문까지|여의도까지"),
]

# 리블에서 그 고민을 푸는 방법 — 실제 기능만
SOLUTION = {
    "compare": "같은 면적으로 맞춰 실거래·전세가율·세대수·연식을 나란히 비교한 표예요",
    "school": "그 학교 주변 단지들 실거래를 한 페이지에 모아 둔 곳이에요(배정은 교육청 확인 필요)",
    "price": "그 단지 평형별 실거래 흐름이랑 최근 거래를 날짜·층까지 볼 수 있어요",
    "jeonse": "그 단지 매매·전세 실거래랑 전세가율을 같이 볼 수 있어요",
    "rebuild": "그 단지 연식·세대수·실거래 흐름을 한 번에 볼 수 있어요",
    "commute": "그 역 주변 단지들 실거래를 모아 둔 페이지예요",
}

# 질문을 짚는 첫 마디 — 커뮤니티 해요체, 짧게
OPENER = {
    "compare": "같은 평형 기준으로 놓고 보면 판단이 좀 쉬워요.",
    "school": "학교 기준으로 주변 단지 시세 보시는 거면,",
    "price": "최근 실거래 기준으로 보면",
    "jeonse": "전세 쪽은 매매랑 같이 보셔야 감이 와요.",
    "rebuild": "연식이랑 거래 흐름 같이 보시면 좋아요.",
    "commute": "역 기준으로 주변 단지 보시는 거면,",
}


def detect(text: str) -> list[str]:
    """신호가 걸린 의도를 우선순위 순으로. 아무것도 없으면 빈 목록."""
    return [name for name, rx in RULES if re.search(rx, text or "")]


# ── 채널 말투 ─────────────────────────────────────────────────────────────
# 카페·지식iN·당근·82cook = 해요체 / 디시·에펨·더쿠 등 = 짧은 음슴체 / 유튜브 = 링크 없이 한 줄(링크 댓글은 스팸 보류가 잦다)
CASUAL = {"dcinside", "fmkorea", "theqoo", "mlbpark", "ruliweb", "instiz", "bobaedream", "clien", "ppomppu", "ppomppu_web", "blind"}
VIDEO = {"youtube"}
_CASUAL_SUB = [("이에요.", "임."), ("예요.", "임."), ("헷갈려요.", "헷갈림."), ("쉬워요.", "쉬움."), ("있어요.", "있음."),
               ("좋아요.", "좋음."), ("와요.", "옴."), ("이고", "이고"), ("보셔야", "봐야"), ("보시면", "보면"),
               ("보시는", "보는"), ("하세요", "해")]


def style(channel: str) -> str:
    return "video" if channel in VIDEO else "casual" if channel in CASUAL else "polite"


def restyle(text: str, channel: str) -> str:
    if style(channel) != "casual":
        return text
    for a, b in _CASUAL_SUB:
        text = text.replace(a, b)
    return text
