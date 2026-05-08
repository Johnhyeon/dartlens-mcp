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


def extract_document_tables(xml_bytes: bytes) -> list[DocumentTable]:
    """Extract table rows/cells from a DART document XML payload."""
    tables: list[DocumentTable] = []
    for payload in _xml_payloads(xml_bytes):
        root = _parse_xml(payload)
        if root is None:
            continue
        for element in root.iter():
            if _tag_name(element) != "table":
                continue
            rows = _extract_rows(element)
            if rows:
                tables.append(DocumentTable(caption=_table_caption(element), rows=rows))
    return tables


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
    rows: list[list[str]] = []
    for tr in table.iter():
        if _tag_name(tr) != "tr":
            continue
        cells = [_cell_text(cell) for cell in tr if _tag_name(cell) in {"td", "th"}]
        cells = [cell for cell in cells if cell]
        if cells:
            rows.append(cells)
    return rows


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
    return " ".join(part.strip() for part in element.itertext() if part and part.strip())


def _tag_name(element) -> str:
    tag = getattr(element, "tag", "")
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()
