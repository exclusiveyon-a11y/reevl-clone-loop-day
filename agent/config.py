"""비밀값은 리블 본체에만 둔다. 실행할 때 환경 변수 → 리블 .env 순서로 필요한 키만 읽는다.

REEVL_ENV(기본 ~/reevl/.env) 파일을 통째로 내보내지 않고, 요청한 이름 하나만 꺼낸다.
"""
from __future__ import annotations

import os
from pathlib import Path

ENV_FILE = Path(os.environ.get("REEVL_ENV", str(Path.home() / "reevl/.env")))


def get(key: str) -> str | None:
    if os.environ.get(key):
        return os.environ[key]
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == key:
                return v.strip().strip("\"'")
    return None
