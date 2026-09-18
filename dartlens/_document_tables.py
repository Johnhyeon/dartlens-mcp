"""Helpers for preserving table structure from DART document XML."""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass

from lxml import etree


@dataclass(frozen=True)
class DocumentTable:
    caption: str
    rows: list[list[str]]
    basis: str = ""    # "연결" / "별도" — 이 표가 실린 재무제표 기준. 모르면 ""
    unit_hint: str = ""  # 표 바로 앞에 따로 적힌 '(단위 : ...)' 쪽지


def extract_document_tables(xml_bytes: bytes) -> list[DocumentTable]:
    """Extract table rows/cells from a DART document XML payload."""
    tables: list[DocumentTable] = []
    for payload in _xml_payloads(xml_bytes):
        root = _parse_xml(payload)
        if root is None:
            continue
        document_label = _document_label(root)
        for element in root.iter():
            if _tag_name(element) != "table":
                continue
            rows = _extract_rows(element)
            if rows:
                tables.append(DocumentTable(
                    caption=_table_caption(element),
                    rows=rows,
                    basis=_financial_basis(element, document_label),
                    unit_hint=_nearby_unit_note(element),
                ))
    return tables


_UNIT_NOTE_RE = re.compile(r"\(\s*단위\s*[:：][^)]{0,40}\)")


def _nearby_unit_note(table) -> str:
    """표 바로 앞에 따로 적힌 '(단위 : ...)' 쪽지.

    DART 원문은 단위를 본 표에 안 쓰고 바로 위에 한 줄짜리 표로 따로 얹는 일이
    잦다. 실측(일진전기 2025 사업보고서 20260311004216): '다. 수주상황' 아래
    '(단위 : 천USD )' 만 든 1행 표가 있고 그 다음이 데이터 표다. 캡션은 중첩
    표를 읽지 않으므로(다른 표 글자가 섞이면 안 되니까) 이 쪽지를 놓쳤고,
    단위 미상으로 여덟 기간이 통째로 빠졌다.

    앞 표의 본문까지 거슬러 올라가지는 않는다 - 남의 표 단위를 물려받으면
    100배 어긋난다.
    """
    previous = table.getprevious()
    checked = 0
    while previous is not None and checked < 2:
        if _holds_data_table(previous):
            return ""
        text = " ".join(part.strip() for part in previous.itertext()
                        if part and part.strip())
        match = _UNIT_NOTE_RE.search(text)
        if match:
            return match.group(0)
        if text:
            checked += 1
        previous = previous.getprevious()
    return ""


def _holds_data_table(element) -> bool:
    """이 요소가 (단위 쪽지가 아니라) 실제 데이터 표를 품고 있는가."""
    for node in element.iter():
        if _tag_name(node) != "table":
            continue
        if sum(1 for child in node.iter() if _tag_name(child) == "tr") > 3:
            return True
    return False


def _document_label(root) -> str:
    """이 XML 파일이 무슨 문서인지 - '사업보고서'·'감사보고서'·'연결감사보고서'."""
    for element in root.iter():
        if _tag_name(element) in {"document-name", "title"}:
            text = _cell_text(element)
            if text:
                return text[:40]
    return ""


def _financial_basis(element, document_label: str) -> str:
    """이 표가 연결 기준인지 별도 기준인지.

    같은 계약잔액 표가 한 보고서에 연결·별도 두 벌 실린다. 어느 쪽이 먼저
    나오는지는 보고서마다 다르다 - 첨부(감사보고서/연결감사보고서)가 본문보다
    앞에 오는 해가 있다. 기준을 안 들고 다니면 2024년은 연결, 2025년은 별도가
    한 시계열에 섞여 있지도 않은 증감이 만들어진다(실측: 현대무벡스).
    """
    text = (_nearest_section_title(element) + " " + document_label).replace(" ", "")
    if "연결" in text:
        return "연결"
    if "재무제표" in text or "재무상태표" in text or "감사보고서" in text:
        return "별도"
    return ""


def _nearest_section_title(element) -> str:
    """이 표를 감싼 가장 가까운 제목 하나. 위로 올라가며 모으면 안 된다 -
    '5. 재무제표 주석'의 조상 위에는 '3. 연결재무제표 주석'이 있다."""
    node = element
    while node is not None:
        previous = node.getprevious()
        while previous is not None:
            if _tag_name(previous) == "title":
                text = _cell_text(previous)
                if text:
                    return text[:60]
            previous = previous.getprevious()
        node = node.getparent()
    return ""


def _xml_payloads(raw: bytes) -> list[bytes]:
    if raw[:2] != b"PK":
        return [raw]
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            xml_names = [name for name in zf.namelist() if name.lower().endswith(".xml")]
            return [zf.read(name) for name in xml_names]
    except Exception:
        return []


def _parse_xml(xml_bytes: bytes):
    try:
        parser = etree.XMLParser(recover=True, huge_tree=True)
        return etree.fromstring(xml_bytes, parser=parser)
    except Exception:
        return None


def _extract_rows(table) -> list[list[str]]:
    """이 표가 직접 가진 행만, 원문의 열 자리 그대로 읽는다.

    병합된 칸(COLSPAN/ROWSPAN)을 펴서 모든 행의 열 번호를 맞춘다. 예전엔 빈 칸을
    지우고 병합을 무시해서, 같은 표 안에서도 행마다 칸 수가 달랐다 - 실측(현대건설
    2025 사업보고서 20260318001395)에서 헤더 8칸, 개별 공사 행 7칸(구분 열이
    세로 병합), 합계 행 4칸(가로 병합)이 섞였다. 그러면 계약잔액 열 번호가 행마다
    달라, 항등식이 찾아낸 열이 합계 행에는 아예 없어 조용히 빠진다. 그 표의
    '국내 / 해외 합계' 69.7조가 빠지고 개별 공사만 더한 22.7조가 나갔다.

    DART 원문은 레이아웃용 껍데기 <TABLE> 안에 실제 표 수백 개를 넣어 보낸다.
    예전엔 table.iter() 가 그 안쪽 <TR> 을 전부 긁어와 서로 상관없는 표
    수백 개가 한 표(947행)로 뭉쳤다. 그러면 어떤 행의 머리글이 100행 위
    다른 표의 행이 될 수 있다 - 실측(현대무벡스 2025 사업보고서
    20260318001359)에서 '기초 계약잔액' 행이 102행 위 주식선택권
    '행사가능시점' 행을 머리글로 잡아, 부문(IT/물류/합계)을
    연도(2025/2026/2027)로 내보냈다. 캡션도 100행 위 손실충당금 표 것이 붙었다.
    """
    rows: list[list[str]] = []
    spans: dict[int, list] = {}      # 세로로 이어지는 칸: 열 -> [남은 행 수, 글자]
    for tr in table.iter():
        if _tag_name(tr) != "tr":
            continue
        if _owner_table(tr) is not table:
            continue
        cells = [cell for cell in tr if _tag_name(cell) in {"td", "th"}]
        row: list[str] = []
        col = 0
        index = 0
        while index < len(cells) or col in spans:
            if col in spans:
                remaining, text = spans[col]
                row.append(text)
                if remaining <= 1:
                    del spans[col]
                else:
                    spans[col] = [remaining - 1, text]
                col += 1
                continue
            cell = cells[index]
            index += 1
            text = _cell_text(cell)
            colspan = _span(cell, "colspan")
            rowspan = _span(cell, "rowspan")
            for offset in range(colspan):
                # 가로로 합쳐진 칸은 첫 자리에만 글자를 두고 나머지는 빈 칸으로
                # 채운다. 자리를 채워야 아래 행의 열 번호와 맞는다.
                row.append(text if offset == 0 else "")
                if rowspan > 1:
                    spans[col] = [rowspan - 1, text if offset == 0 else ""]
                col += 1
        if any(cell.strip() for cell in row):
            rows.append(row)
    return rows


_MAX_SPAN = {"colspan": 40, "rowspan": 200}


def _span(cell, name: str) -> int:
    """COLSPAN/ROWSPAN 값. 레이아웃용 큰 값은 잘라 표가 터지지 않게 한다."""
    raw = cell.get(name.upper()) or cell.get(name) or "1"
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return 1
    return max(1, min(value, _MAX_SPAN[name]))


def _owner_table(node):
    """이 행을 직접 담고 있는 표. 중첩 표 안의 행은 바깥 표의 것이 아니다."""
    parent = node.getparent()
    while parent is not None:
        if _tag_name(parent) == "table":
            return parent
        parent = parent.getparent()
    return None


def _table_caption(table) -> str:
    for child in table:
        if _tag_name(child) == "caption":
            text = _cell_text(child)
            if text:
                return text

    previous = table.getprevious()
    while previous is not None:
        text = _cell_text(previous)
        if text:
            return text
        previous = previous.getprevious()
    return ""


def _cell_text(element) -> str:
    """이 요소의 글자만. 중첩된 표의 글자는 그 표의 것이므로 가져오지 않는다.

    껍데기 <TABLE> 의 셀 하나가 실제 표 수백 개를 품고 있어서, 예전엔 그 셀
    하나의 글자가 섹션 전체가 됐다(캡션이 남의 표 문장으로 찍힌 이유).
    """
    if not isinstance(getattr(element, "tag", None), str):
        return ""
    parts: list[str] = []
    _collect_text(element, parts, root=element)
    return " ".join(parts)


def _collect_text(node, parts: list[str], *, root) -> None:
    if not isinstance(getattr(node, "tag", None), str):
        return
    if node is not root and _tag_name(node) == "table":
        return
    if node.text and node.text.strip():
        parts.append(node.text.strip())
    for child in node:
        _collect_text(child, parts, root=root)
        if child.tail and child.tail.strip():
            parts.append(child.tail.strip())


def _tag_name(element) -> str:
    tag = getattr(element, "tag", "")
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()
