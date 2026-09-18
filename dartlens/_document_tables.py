"""Helpers for preserving table structure from DART document XML."""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass

from lxml import etree


@dataclass(frozen=True)
class DocumentTable:
    caption: str
    rows: list[list[str]]
    basis: str = ""    # "연결" / "별도" — 이 표가 실린 재무제표 기준. 모르면 ""


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
                ))
    return tables


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
    """이 표가 직접 가진 행만 읽는다 - 중첩된 표의 행은 그 표의 것이다.

    DART 원문은 레이아웃용 껍데기 <TABLE> 안에 실제 표 수백 개를 넣어 보낸다.
    예전엔 table.iter() 가 그 안쪽 <TR> 을 전부 긁어와 서로 상관없는 표
    수백 개가 한 표(947행)로 뭉쳤다. 그러면 어떤 행의 머리글이 100행 위
    다른 표의 행이 될 수 있다 - 실측(현대무벡스 2025 사업보고서
    20260318001359)에서 '기초 계약잔액' 행이 102행 위 주식선택권
    '행사가능시점' 행을 머리글로 잡아, 부문(IT/물류/합계)을
    연도(2025/2026/2027)로 내보냈다. 캡션도 100행 위 손실충당금 표 것이 붙었다.
    """
    rows: list[list[str]] = []
    for tr in table.iter():
        if _tag_name(tr) != "tr":
            continue
        if _owner_table(tr) is not table:
            continue
        cells = [_cell_text(cell) for cell in tr if _tag_name(cell) in {"td", "th"}]
        cells = [cell for cell in cells if cell]
        if cells:
            rows.append(cells)
    return rows


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
