"""TTL 캐시 — 공시는 사실상 불변(정정공시 제외)이라 길게 잡는다.

기본 정책:
- 공시 목록 / 검색 결과: 5분 (신규 공시 반영 위해 짧게)
- 공시 본문 / 재무제표 / 기업개황: 24시간 (불변에 가까움)

stocklens처럼 장중/장마감 구분이 필요 없다 — 공시는 시간대 무관.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Awaitable, Callable

_cache: dict[str, tuple[float, Any]] = {}
_lock = asyncio.Lock()


def _make_key(func_name: str, args: tuple, kwargs: dict) -> str:
    parts = [func_name]
    parts.extend(repr(a) for a in args)
    parts.extend(f"{k}={v!r}" for k, v in sorted(kwargs.items()))
    return "|".join(parts)


def cached(ttl_seconds: int):
    """async 함수 결과를 고정 TTL로 캐싱."""

    def decorator(func: Callable[..., Awaitable[Any]]):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            key = _make_key(func.__name__, args, kwargs)

            async with _lock:
                entry = _cache.get(key)
                if entry is not None:
                    expiry, value = entry
                    if time.time() < expiry:
                        return value
                    del _cache[key]

            result = await func(*args, **kwargs)

            async with _lock:
                _cache[key] = (time.time() + ttl_seconds, result)

            return result

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# 영구 캐시 (SQLite) — scan_earnings_season 전용
#
# 정기보고서 확정 데이터는 분기 마감 후 변하지 않는다 → TTL 없이 영구 보존.
# 어닝 시즌에 매일 호출해도 캐시 hit한 corp는 API 호출을 스킵하고,
# 신규 접수분만 증분으로 fetch한다. sqlite3는 동기 API라 호출 측에서
# asyncio.to_thread로 감싸 이벤트 루프 블로킹을 피한다.
# ---------------------------------------------------------------------------


class EarningsCache:
    """(corp_code, bsns_year, reprt_code, fs_div) → 핵심계정 dict 영구 KV.

    값은 호출 측이 추출한 {rev_cur, op_cur, ni_cur, rev_prev, op_prev,
    ni_prev, corp_name} 형태의 dict. JSON 직렬화해 한 컬럼에 저장한다.
    데이터가 있었던 corp만 저장한다 — 아직 미접수한 corp는 저장하지 않아
    다음 호출 때 재시도된다(어닝 시즌 증분 처리의 핵심).
    """

    def __init__(self, path: str | Path):
        self._path = str(path)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS earnings ("
            " key TEXT PRIMARY KEY,"
            " payload TEXT NOT NULL,"
            " fetched_at TEXT NOT NULL)"
        )
        self._conn.commit()

    @staticmethod
    def make_key(corp_code: str, bsns_year: int | str, reprt_code: str, fs_div: str) -> str:
        return f"{corp_code}|{bsns_year}|{reprt_code}|{fs_div}"

    def get_many(self, keys: list[str]) -> dict[str, dict]:
        if not keys:
            return {}
        out: dict[str, dict] = {}
        # SQLite 변수 한도(999) 회피 위해 500개씩 청크
        for i in range(0, len(keys), 500):
            chunk = keys[i : i + 500]
            placeholders = ",".join("?" * len(chunk))
            rows = self._conn.execute(
                f"SELECT key, payload FROM earnings WHERE key IN ({placeholders})",
                chunk,
            ).fetchall()
            for k, payload in rows:
                try:
                    out[k] = json.loads(payload)
                except (ValueError, TypeError):
                    pass
        return out

    def set_many(self, items: dict[str, dict]) -> None:
        if not items:
            return
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._conn.executemany(
            "INSERT OR REPLACE INTO earnings (key, payload, fetched_at) VALUES (?,?,?)",
            [
                (k, json.dumps(v, ensure_ascii=False), now)
                for k, v in items.items()
            ],
        )
        self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


_earnings_cache: EarningsCache | None = None


def get_earnings_cache() -> EarningsCache:
    """프로세스 전역 EarningsCache 싱글톤. ~/.dartlens/cache/earnings.sqlite."""
    global _earnings_cache
    if _earnings_cache is None:
        # 지연 import — _metrics는 무거운 경로 의존이 없지만 순환 회피 위해 함수 내부
        from dartlens._metrics import get_data_dir

        cache_dir = get_data_dir() / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        _earnings_cache = EarningsCache(cache_dir / "earnings.sqlite")
    return _earnings_cache


def clear_cache() -> None:
    _cache.clear()


def cache_stats() -> dict:
    now = time.time()
    active = sum(1 for exp, _ in _cache.values() if exp > now)
    return {
        "total_entries": len(_cache),
        "active_entries": active,
        "expired_entries": len(_cache) - active,
    }
