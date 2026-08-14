"""DART corpCode.xml 다운로드 / 캐시 / 종목코드↔corp_code 매핑.

DART OpenAPI는 corp_code(8자리 고유번호)로만 회사를 식별한다.
사용자/Claude는 보통 종목명("삼성전자") 또는 종목코드("005930")로 묻기 때문에
corpCode.xml을 한 번 받아 로컬에 캐시하고 이름/코드로 lookup할 수 있어야 한다.

corpCode.xml 다운로드는 약 1~3MB zip이고 일 단위로 갱신된다 → 7일 TTL로 캐시.
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import shutil
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from lxml import etree

from dartlens._http import BULK_TIMEOUT, get_bytes
from dartlens._metrics import _error_detail, get_data_dir

_CACHE_FILE = "corpCode.xml"
_TTL_SECONDS = 7 * 24 * 3600  # 7일

# 메모리 인덱스 (프로세스 lifetime 동안 유지)
_lock = asyncio.Lock()
_loaded_at: float = 0.0
# 직전 실패 기억 — 연쇄 재시도로 사용자를 오래 붙잡아두지 않기 위한 것. 짧게 잡는다:
# 원인이 사라지면(네트워크 복구 등) 곧 다시 시도해야 하고, 사용자가 --repair 로
# 직접 고치는 경로는 이 값을 아예 무시한다.
_FAILURE_COOLDOWN_SECONDS = 60.0
_failed_at: float = 0.0
_failure_reason: str = ""
_by_corp_code: dict[str, "CorpEntry"] = {}
_by_stock_code: dict[str, "CorpEntry"] = {}
# 정확 일치 / 부분 일치 검색을 위한 (정규화된 이름) → entries
_by_name_lower: dict[str, list["CorpEntry"]] = {}


@dataclass(frozen=True)
class CorpEntry:
    corp_code: str       # 8자리
    corp_name: str
    corp_eng_name: str
    stock_code: str      # 상장사면 6자리, 비상장사는 ""
    modify_date: str     # YYYYMMDD

    @property
    def is_listed(self) -> bool:
        return bool(self.stock_code and self.stock_code.strip())

    def to_dict(self) -> dict:
        return {
            "corp_code": self.corp_code,
            "corp_name": self.corp_name,
            "corp_eng_name": self.corp_eng_name,
            "stock_code": self.stock_code,
            "modify_date": self.modify_date,
            "is_listed": self.is_listed,
        }


def _cache_path() -> Path:
    cache_dir = get_data_dir() / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / _CACHE_FILE


def _is_cache_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    age = time.time() - path.stat().st_mtime
    return age < _TTL_SECONDS


async def _download_corp_code() -> bytes:
    """DART에서 corpCode.xml zip을 받아 내부 XML 바이트 반환.

    벌크 전용 타임아웃을 쓴다(BULK_TIMEOUT) — 3.4MB짜리라 작은 JSON 기준 제한으로는
    지연이 큰 회선에서 다 받기 전에 잘린다. 재시도는 1회로 줄인다: 한 번에 2분까지
    기다릴 수 있으므로 기본값(2회)이면 최악의 경우 6분을 붙잡고 있게 된다.
    """
    raw = await get_bytes("/corpCode.xml", timeout=BULK_TIMEOUT, max_retries=1)
    if raw[:2] != b"PK":
        # 예전엔 zip이 아니면 "에러 응답이 XML로 올 수 있다"며 그대로 돌려줬다. 그게
        # 호출 측에서 캐시로 굳었다 — DART의 에러 XML은 lxml로 잘 파싱되고 <list>가
        # 없을 뿐이라, 예외 없이 '기업 0곳'짜리 캐시가 만들어지고 7일 동안 유지됐다.
        # 그 사이 회사 조회는 전부 실패하는데 doctor는 "최신 상태"라고 말한다.
        # 정상 응답은 항상 zip이다 — 아니면 실패로 다룬다.
        snippet = _error_detail_from_bytes(raw)
        raise RuntimeError(f"DART가 기업코드 파일 대신 다른 응답을 보냈습니다: {snippet}")
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        # 보통 'CORPCODE.xml' 단일 파일
        names = zf.namelist()
        if not names:
            raise RuntimeError("corpCode zip이 비어있습니다.")
        with zf.open(names[0]) as fp:
            return fp.read()


def _parse_xml(xml_bytes: bytes) -> list[CorpEntry]:
    root = etree.fromstring(xml_bytes)
    entries: list[CorpEntry] = []
    for node in root.iterfind("list"):
        entries.append(
            CorpEntry(
                corp_code=(node.findtext("corp_code") or "").strip(),
                corp_name=(node.findtext("corp_name") or "").strip(),
                corp_eng_name=(node.findtext("corp_eng_name") or "").strip(),
                stock_code=(node.findtext("stock_code") or "").strip(),
                modify_date=(node.findtext("modify_date") or "").strip(),
            )
        )
    return entries


def _build_indexes(entries: list[CorpEntry]) -> None:
    global _by_corp_code, _by_stock_code, _by_name_lower
    by_corp: dict[str, CorpEntry] = {}
    by_stock: dict[str, CorpEntry] = {}
    by_name: dict[str, list[CorpEntry]] = {}
    for e in entries:
        if e.corp_code:
            by_corp[e.corp_code] = e
        if e.is_listed:
            by_stock[e.stock_code] = e
        if e.corp_name:
            by_name.setdefault(e.corp_name.lower(), []).append(e)
    _by_corp_code = by_corp
    _by_stock_code = by_stock
    _by_name_lower = by_name


# 응답 본문 전용 마스킹. _metrics._error_detail 은 URL용이라 `?` 뒤를 통째로 지우는데,
# 그걸 XML 본문에 쓰면 `<?xml ...?>` 선언에 걸려 뒤따르는 <status>013</status> 까지
# 잘려나간다 — 하필 진단에 제일 필요한 값이다. 본문에 실릴 수 있는 건 (에러 페이지가
# 요청 URL을 되비추는 경우의) crtfc_key 뿐이므로 그 값만 지운다.
_BODY_SECRET_RE = re.compile(r"(crtfc_key\s*=\s*)([0-9A-Za-z]{4,})", re.IGNORECASE)


def _error_detail_from_bytes(raw: bytes, limit: int = 200) -> str:
    """받은 응답이 무엇이었는지 사람이 읽을 수 있게. 크리덴셜은 지우고 담는다."""
    if not raw:
        return "빈 응답"
    text = raw[:limit].decode("utf-8", errors="replace").strip()
    text = " ".join(text.split())
    return _BODY_SECRET_RE.sub(lambda m: f"{m.group(1)}***", text)


async def ensure_loaded(force_refresh: bool = False) -> None:
    """corpCode.xml을 디스크 캐시에서 로드하거나, 만료/없으면 다운로드.

    검증을 통과한 것만 디스크에 남긴다. 예전엔 받은 바이트를 먼저 쓰고 나중에
    파싱해서, 에러 응답이 그대로 캐시가 되어 '기업 0곳'인 채로 7일을 버텼다.
    이미 그렇게 굳은 캐시(옛 버전이 만든 것)도 0건이면 무시하고 다시 받는다 —
    재설치로는 안 풀리는 자리라(캐시는 패키지 밖에 있다) 스스로 벗어나야 한다.

    직전 실패는 잠깐 기억한다. 이 함수는 락을 쥔 채로 3.4MB를 받으므로, 실패가
    이어지는 상황에서 호출마다 처음부터 다시 받으면 Claude가 도구를 몇 번만 불러도
    그 시간이 직렬로 쌓인다(측정: 5회 호출 = 5회 재다운로드, 최악 20분). 안 되는
    걸 오래 기다리게 하느니 같은 이유로 빨리 실패하는 편이 낫다. 사용자가 직접
    고치려는 경우(force_refresh, 즉 --repair)는 이 기억을 무시하고 반드시 시도한다.
    """
    global _loaded_at, _failed_at, _failure_reason

    async with _lock:
        if _loaded_at and not force_refresh:
            return

        if (
            not force_refresh
            and _failed_at
            and (time.time() - _failed_at) < _FAILURE_COOLDOWN_SECONDS
        ):
            raise RuntimeError(
                f"기업코드 파일을 조금 전에 받지 못했습니다 — {_failure_reason} "
                "(잠시 후 다시 시도해주세요)"
            )

        path = _cache_path()
        entries: list[CorpEntry] = []
        stale: list[CorpEntry] = []

        if not force_refresh:
            try:
                parsed = _parse_xml(path.read_bytes())
            except Exception:
                parsed = []  # 없거나 손상됨 — 아래에서 받는다
            if parsed:
                if _is_cache_fresh(path):
                    entries = parsed
                else:
                    stale = parsed  # 기한은 지났지만 쓸 수는 있다

        if not entries:
            try:
                xml_bytes = await _download_corp_code()
                entries = _parse_xml(xml_bytes)
                if not entries:
                    raise RuntimeError(
                        "받은 기업코드 파일에 기업이 한 곳도 들어있지 않습니다."
                    )
            except BaseException as e:  # CancelledError 도 실패로 기억한다
                _failed_at = time.time()
                _failure_reason = f"{type(e).__name__}: {e}"[:200]
                # 갱신에 실패했어도 디스크에 쓸 수 있는 목록이 있으면 그걸 쓴다.
                # 기업코드는 천천히 바뀌므로 며칠 지난 목록이 '아무것도 없음'보다
                # 훨씬 낫다. 예전엔 기한(7일)만 보고 버려서, 한 번 받아둔 사람도
                # 딱 7일 뒤에 같은 장애를 다시 겪게 돼 있었다(2026-08-14 문의에서
                # --repair 로 겨우 살린 PC가 그대로 이 시한폭탄을 안고 있었다).
                # doctor 는 이 상태를 "캐시가 오래되었습니다"로 계속 알린다.
                if stale:
                    _build_indexes(stale)
                    _loaded_at = time.time()
                    return
                raise
            path.write_bytes(xml_bytes)

        _build_indexes(entries)
        _loaded_at = time.time()
        _failed_at = 0.0
        _failure_reason = ""


# 외부 노출 lookup --------------------------------------------------------

async def lookup_by_corp_code(corp_code: str) -> CorpEntry | None:
    await ensure_loaded()
    return _by_corp_code.get(corp_code.strip())


async def lookup_by_stock_code(stock_code: str) -> CorpEntry | None:
    await ensure_loaded()
    return _by_stock_code.get(stock_code.strip())


async def search_by_name(
    query: str,
    *,
    listed_only: bool = True,
    limit: int = 20,
) -> list[CorpEntry]:
    """이름으로 검색. 정확 일치 우선, 그 다음 부분 일치."""
    await ensure_loaded()
    q = query.strip().lower()
    if not q:
        return []

    exact = _by_name_lower.get(q, [])
    partial: list[CorpEntry] = []
    if len(exact) < limit:
        for name, entries in _by_name_lower.items():
            if name == q:
                continue
            if q in name:
                partial.extend(entries)
                if len(exact) + len(partial) >= limit * 3:
                    break

    combined = exact + partial
    if listed_only:
        combined = [e for e in combined if e.is_listed]

    # 같은 corp_code 중복 제거 (이름 동일한 다회사 케이스)
    seen: set[str] = set()
    out: list[CorpEntry] = []
    for e in combined:
        if e.corp_code in seen:
            continue
        seen.add(e.corp_code)
        out.append(e)
        if len(out) >= limit:
            break
    return out


async def all_listed() -> list[CorpEntry]:
    """상장사(stock_code 보유) 전체 엔트리. universe='all' 해석용.

    corpCode.xml에는 시장 구분(KOSPI/KOSDAQ)이 없으므로 호출 측에서
    kospi/kosdaq 요청 시 이 함수로 폴백하고 footer에 경고를 남긴다.
    """
    await ensure_loaded()
    return [e for e in _by_corp_code.values() if e.is_listed]


async def corp_name_map(corp_codes: list[str]) -> dict[str, str]:
    """corp_code 리스트 → {corp_code: corp_name}. 없는 코드는 생략.

    fnlttMultiAcnt 응답에는 corp_name이 없어 스캐닝 결과의 회사명을
    corpCode.xml 인덱스에서 일괄 해석하는 데 쓴다 (코드당 dict 조회 O(1)).
    """
    await ensure_loaded()
    out: dict[str, str] = {}
    for cc in corp_codes:
        e = _by_corp_code.get(cc.strip())
        if e and e.corp_name:
            out[cc] = e.corp_name
    return out


async def corp_basic_map(corp_codes: list[str]) -> dict[str, tuple[str, str]]:
    """corp_code 리스트 → {corp_code: (corp_name, stock_code)}. 없는 코드 생략.

    scan 결과에 회사명 + KRX 메타(시장/업종/주요제품, stock_code 키) 조인용.
    """
    await ensure_loaded()
    out: dict[str, tuple[str, str]] = {}
    for cc in corp_codes:
        e = _by_corp_code.get(cc.strip())
        if e:
            out[cc] = (e.corp_name, e.stock_code)
    return out


async def resolve_identifier(identifier: str) -> CorpEntry | None:
    """입력이 corp_code(8자리)인지 stock_code(6자리)인지 자동 판정해서 1건 반환."""
    s = identifier.strip()
    if len(s) == 8 and s.isdigit():
        return await lookup_by_corp_code(s)
    if len(s) == 6 and s.isalnum():
        return await lookup_by_stock_code(s)
    return None


# ---------------------------------------------------------------------------
# 캐시 진단 / repair (dartlens-doctor 용)
# ---------------------------------------------------------------------------


def cache_diagnosis() -> dict:
    """corp code 캐시 진단 — 존재/최신성/파싱 가능 여부/기업 수/쓰기 권한.

    순수 로컬 파일 I/O만 수행한다 (다운로드 없음) — doctor 기본(오프라인) 모드에서도 안전.
    """
    path = _cache_path()
    info: dict = {
        "exists": path.exists(),
        "last_updated": None,
        "is_fresh": False,
        "entry_count": None,
        "parseable": None,
        "writable": os.access(path.parent, os.W_OK) if path.parent.exists() else False,
    }
    if not info["exists"]:
        return info

    info["last_updated"] = datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
    info["is_fresh"] = _is_cache_fresh(path)
    try:
        entries = _parse_xml(path.read_bytes())
        info["parseable"] = True
        info["entry_count"] = len(entries)
    except Exception:
        info["parseable"] = False
        info["entry_count"] = 0
    return info


def repair_corp_code_cache(*, yes: bool) -> dict:
    """corp-code-cache repair — 기존 캐시를 `.bak`으로 보존한 뒤 강제 재다운로드.

    yes=False면 아무 파일도 건드리지 않고 확인 필요 메시지만 반환한다 (파괴적 작업 안전장치).
    """
    if not yes:
        return {"repaired": False, "message": "재다운로드하려면 --yes 플래그가 필요합니다."}

    path = _cache_path()
    try:
        if path.exists():
            bak = path.with_suffix(path.suffix + ".bak")
            shutil.copy2(path, bak)
        asyncio.run(ensure_loaded(force_refresh=True))
    except Exception as e:
        # `{e}` 를 그대로 쓰면 안 된다 — httpx 예외 문자열엔 요청 URL이 통째로 들어가고
        # DART 호출 URL에는 `?crtfc_key=<40자리>` 가 실린다. 이 메시지는 화면에 뜨고
        # doctor --json 으로 지원 번들에도 나간다.
        return {"repaired": False, "message": f"재다운로드 실패: {type(e).__name__}: {_error_detail(e)}"}

    diag = cache_diagnosis()
    return {"repaired": True, "entry_count": diag["entry_count"], "last_updated": diag["last_updated"]}
