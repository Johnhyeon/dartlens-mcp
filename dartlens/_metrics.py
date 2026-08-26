"""MCP 도구 호출 메트릭 (JSONL).

저장 위치: ~/.dartlens/logs/metrics_YYYYMMDD.jsonl

stocklens 메트릭과 호환되는 스키마로 기록한다 (timestamp/tool/duration_ms/output_chars/error).
"""

from __future__ import annotations

import json
import re
import time
from contextvars import ContextVar
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
            token = _current_tool.set(tool_name)
            start = time.monotonic()
            error_type: str | None = None
            error_detail: str | None = None
            result_text = ""
            try:
                result = await func(*args, **kwargs)
                if result is not None:
                    result_text = str(result)
                return result
            # BaseException 까지 잡는 이유: asyncio.CancelledError 는 Exception 이
            # 아니라 BaseException 이다. 클라이언트가 느린 호출을 취소하면 예전엔
            # error=null, output_chars=0 으로 기록돼 **성공한 것처럼** 보였다.
            # corpCode.xml(3.4MB)은 취소될 여지가 실제로 있다. 기록만 하고 그대로
            # 다시 올린다(흐름은 바뀌지 않는다).
            except BaseException as e:
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
                # 도구 밖에서 current_tool() 이 남아 있지 않게 되돌린다.
                _current_tool.reset(token)

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# 커버리지 한계 카운터 (메타 규약 v3)
# ---------------------------------------------------------------------------
# 도구가 요청보다 적게 돌려주는 일이 얼마나 자주 일어나는지 세지 않으면, 계약을
# 만들어 놓고도 "그래서 실제로 얼마나 잘리나"에 답할 수 없다.
#
# 라벨은 고정 집합만 받는다. 종목코드·티커·검색어를 라벨로 받으면 두 가지가
# 동시에 깨진다: 시계열 카디널리티가 종목 수만큼 늘어나고, 지원 번들로 나가는
# 로그에 고객이 무엇을 조회했는지가 그대로 남는다.

COUNTER_LABELS: dict[str, tuple[str, ...]] = {
    "lens_coverage_truncated_total": ("lens", "tool", "reason"),
    "lens_incomplete_bar_total": ("tool", "timeframe"),
    "lens_mixed_period_total": ("tool",),
    "lens_unknown_adjustment_total": ("tool",),
}

_LABEL_VALUE_MAX = 40

# 실행 중인 도구 이름. 카운터 라벨 하나 때문에 도구 이름을 40곳 호출부까지
# 인자로 실어 나르지 않으려고 여기 둔다.
_current_tool: ContextVar = ContextVar("lens_current_tool", default=None)


def current_tool():
    """지금 실행 중인 MCP 도구 이름. 도구 밖에서는 None."""
    return _current_tool.get()


def get_counters_file() -> Path:
    """오늘 날짜의 카운터 파일. 도구 호출 기록과 **다른 파일**이다.

    한 파일에 섞으면 기존 파서(load_metrics 등)가 모양이 다른 줄을 만나 조용히
    어긋난다. 지원 번들에는 같은 폴더째로 담기므로 나눠도 함께 나간다.
    """
    date_str = datetime.now().strftime("%Y%m%d")
    return get_metrics_dir() / f"counters_{date_str}.jsonl"


def count_limitation(metric: str, **labels) -> None:
    """커버리지 한계를 1 센다. 허용 라벨 밖의 값은 받지 않는다.

    라벨이 틀리면 조용히 넘어가지 않고 ValueError 로 올린다. 라벨은 전부 코드에
    박힌 상수라, 틀렸다면 그건 버그이지 사용자 입력이 아니다. 파일 쓰기 실패만
    삼킨다 - 메트릭 때문에 도구 호출이 죽으면 안 된다.
    """
    allowed = COUNTER_LABELS.get(metric)
    if allowed is None:
        raise ValueError(f"정의되지 않은 카운터: {metric!r} (허용: {sorted(COUNTER_LABELS)})")
    if set(labels) != set(allowed):
        raise ValueError(
            f"{metric} 의 라벨은 정확히 {sorted(allowed)} 여야 합니다 (받음: {sorted(labels)})"
        )
    for key, value in labels.items():
        if not isinstance(value, str) or not value:
            raise ValueError(f"라벨 {key} 는 비어 있지 않은 문자열이어야 합니다 (받음: {value!r})")
        if len(value) > _LABEL_VALUE_MAX:
            raise ValueError(f"라벨 {key} 가 너무 깁니다({len(value)}자). 자유 문자열은 라벨이 아닙니다.")

    record = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "metric": metric,
        "labels": {k: labels[k] for k in allowed},
        "value": 1,
    }
    try:
        with open(get_counters_file(), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass
