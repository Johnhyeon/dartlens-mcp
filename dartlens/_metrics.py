"""MCP 도구 호출 메트릭 (JSONL).

저장 위치: ~/.dartlens/logs/metrics_YYYYMMDD.jsonl

stocklens 메트릭과 호환되는 스키마로 기록한다 (timestamp/tool/duration_ms/output_chars/error).
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Awaitable, Callable

# stocklens/_metrics.py 와 같은 규칙이어야 한다 — 두 로그를 나란히 놓고 읽는다.
_QUERY_RE = re.compile(r"\?[^\s'\"]*")
_DETAIL_MAX = 200


def _error_detail(exc: Exception) -> str | None:
    """예외 메시지를 로그에 담을 수 있는 형태로. 메시지가 없으면 None.

    쿼리스트링은 통째로 지운다 — httpx 예외 문자열에는 요청 URL이 그대로 들어가고
    DART 호출 URL에는 `?crtfc_key=<40자리>` 가 실린다. 이 파일은 지원 번들에 담겨
    고객이 메일로 내보낸다.
    """
    msg = str(exc).strip()
    if not msg:
        return None
    return _QUERY_RE.sub("?…", msg)[:_DETAIL_MAX]


def get_data_dir() -> Path:
    """사용자 홈 아래 dartlens 데이터 디렉토리.

    `~/.dartlens` 가 비어있고 legacy `~/.dart-mcp-server` 가 존재하면
    그 경로를 그대로 반환해 기존 캐시(corpCode.xml)와 메트릭을 보존한다.
    """
    folder = Path.home() / ".dartlens"
    legacy = Path.home() / ".dart-mcp-server"
    if not folder.exists() and legacy.exists():
        return legacy
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def get_metrics_dir() -> Path:
    folder = get_data_dir() / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def get_metrics_file() -> Path:
    return get_metrics_dir() / f"metrics_{datetime.now():%Y%m%d}.jsonl"


def _sanitize_kwargs(kwargs: dict) -> dict:
    out = {}
    for k, v in kwargs.items():
        if isinstance(v, (str, int, float, bool, type(None))):
            out[k] = v[:47] + "..." if isinstance(v, str) and len(v) > 50 else v
        elif isinstance(v, (list, tuple)):
            out[k] = f"<list len={len(v)}>"
        elif isinstance(v, dict):
            out[k] = f"<dict keys={list(v.keys())}>"
        else:
            out[k] = f"<{type(v).__name__}>"
    return out


def _dart_call_status_path() -> Path:
    return get_metrics_dir() / "dart_call_status.json"


def record_dart_call(status: str) -> None:
    """DART 응답 status 코드를 마지막 호출 시각과 함께 기록 — 히스토리가 아니라 최신 값만 덮어쓴다.

    `dartlens_status` MCP 도구가 "최근 성공 호출 시각 / 마지막 DART status" 를 보여주는 데 쓴다.
    쓰기 실패는 조용히 무시한다 — 진단 부가기능이 본 API 호출 흐름을 막으면 안 된다
    (기존 track_metrics와 동일한 방어 스타일).
    """
    path = _dart_call_status_path()
    now = datetime.now().isoformat(timespec="seconds")
    try:
        existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        existing = {}
    existing["last_call_at"] = now
    existing["last_status"] = status
    if status in ("000", "013"):  # DART 성공 / 조회결과없음 — 연결 성공으로 취급
        existing["last_success_at"] = now
    try:
        path.write_text(json.dumps(existing, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def read_dart_call_status() -> dict:
    """record_dart_call()이 저장한 마지막 호출 상태. 기록이 없으면 모두 None."""
    path = _dart_call_status_path()
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return {
                "last_call_at": data.get("last_call_at"),
                "last_status": data.get("last_status"),
                "last_success_at": data.get("last_success_at"),
            }
    except Exception:
        pass
    return {"last_call_at": None, "last_status": None, "last_success_at": None}


def track_metrics(tool_name: str) -> Callable:
    def decorator(func: Callable[..., Awaitable[Any]]):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            start = time.monotonic()
            error_type: str | None = None
            error_detail: str | None = None
            result_text = ""
            try:
                result = await func(*args, **kwargs)
                if result is not None:
                    result_text = str(result)
                return result
            except Exception as e:
                error_type = type(e).__name__
                # 타입만 남기면 "ConnectError"가 전부라 원인을 못 좁힌다 — 이름 조회
                # 실패인지·거부인지·프록시인지에 따라 사용자가 할 일이 완전히 다르다
                # (2026-08-13 문의에서 실제로 여기서 막혔다).
                error_detail = _error_detail(e)
                raise
            finally:
                duration_ms = round((time.monotonic() - start) * 1000, 1)
                try:
                    record = {
                        "timestamp": datetime.now().isoformat(timespec="seconds"),
                        "tool": tool_name,
                        "kwargs": _sanitize_kwargs(kwargs),
                        "duration_ms": duration_ms,
                        "output_chars": len(result_text),
                        "cache_hit": duration_ms < 10.0,
                        "error": error_type,
                        "error_detail": error_detail,
                    }
                    with open(get_metrics_file(), "a", encoding="utf-8") as f:
                        f.write(json.dumps(record, ensure_ascii=False) + "\n")
                except Exception:
                    pass

        return wrapper

    return decorator
