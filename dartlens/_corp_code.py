"""DART corpCode.xml 다운로드 / 캐시 / 종목코드↔corp_code 매핑.

DART OpenAPI는 corp_code(8자리 고유번호)로만 회사를 식별한다.
사용자/Claude는 보통 종목명("삼성전자") 또는 종목코드("005930")로 묻기 때문에
corpCode.xml을 한 번 받아 로컬에 캐시하고 이름/코드로 lookup할 수 있어야 한다.

corpCode.xml 다운로드는 약 1~3MB zip이고 일 단위로 갱신된다 → 7일 TTL로 캐시.
"""

from __future__ import annotations

import asyncio
import io
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

from lxml import etree

from dartlens._http import get_bytes
from dartlens._metrics import get_data_dir

_CACHE_FILE = "corpCode.xml"
_TTL_SECONDS = 7 * 24 * 3600  # 7일

# 메모리 인덱스 (프로세스 lifetime 동안 유지)
_lock = asyncio.Lock()
_loaded_at: float = 0.0
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
    """DART에서 corpCode.xml zip을 받아 내부 XML 바이트 반환."""
    raw = await get_bytes("/corpCode.xml")
    # zip 또는 raw XML — DART는 zip으로 응답
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            # 보통 'CORPCODE.xml' 단일 파일
            names = zf.namelist()
            if not names:
                raise RuntimeError("corpCode zip이 비어있습니다.")
            with zf.open(names[0]) as fp:
                return fp.read()
    # 에러 응답이 zip이 아닌 XML로 올 수 있음 — 그냥 반환
    return raw


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


async def ensure_loaded(force_refresh: bool = False) -> None:
    """corpCode.xml을 디스크 캐시에서 로드하거나, 만료/없으면 다운로드."""
    global _loaded_at

    async with _lock:
        if _loaded_at and not force_refresh:
            return

        path = _cache_path()
        if force_refresh or not _is_cache_fresh(path):
            xml_bytes = await _download_corp_code()
            path.write_bytes(xml_bytes)
        else:
            xml_bytes = path.read_bytes()

        entries = _parse_xml(xml_bytes)
        _build_indexes(entries)
        _loaded_at = time.time()


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
