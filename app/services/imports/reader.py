"""Read CSV / XLSX / XLS into plain rows of cells. No interpretation happens here."""

import csv
import io
from dataclasses import dataclass
from typing import Any


class UnsupportedFile(Exception):
    pass


@dataclass
class Sheet:
    name: str
    rows: list[list[Any]]


MAX_ROWS = 20_000


def _trim(rows: list[list[Any]]) -> list[list[Any]]:
    """Drop trailing empty cells on each row; keep empty rows (row numbers must stay true)."""
    out = []
    for row in rows:
        cells = list(row)
        while cells and (
            cells[-1] is None or (isinstance(cells[-1], str) and not cells[-1].strip())
        ):
            cells.pop()
        out.append(cells)
    while out and not out[-1]:
        out.pop()
    return out


def _read_csv(data: bytes) -> list[Sheet]:
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))[:MAX_ROWS]
    return [Sheet("csv", _trim(rows))]


def _read_xlsx(data: bytes) -> list[Sheet]:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    sheets = []
    for ws in wb.worksheets:
        rows = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= MAX_ROWS:
                break
            rows.append(list(row))
        sheets.append(Sheet(ws.title, _trim(rows)))
    wb.close()
    return sheets


def _read_xls(data: bytes) -> list[Sheet]:
    import xlrd

    book = xlrd.open_workbook(file_contents=data)
    sheets = []
    for sh in book.sheets():
        rows = []
        for r in range(min(sh.nrows, MAX_ROWS)):
            cells = []
            for c in range(sh.ncols):
                cell = sh.cell(r, c)
                if cell.ctype == xlrd.XL_CELL_DATE:
                    cells.append(xlrd.xldate.xldate_as_datetime(cell.value, book.datemode))
                elif cell.ctype == xlrd.XL_CELL_EMPTY:
                    cells.append(None)
                else:
                    cells.append(cell.value)
            rows.append(cells)
        sheets.append(Sheet(sh.name, _trim(rows)))
    return sheets


def read_file(filename: str, data: bytes) -> list[Sheet]:
    name = filename.lower()
    try:
        if name.endswith(".csv") or name.endswith(".txt"):
            return _read_csv(data)
        if name.endswith(".xlsx") or name.endswith(".xlsm"):
            return _read_xlsx(data)
        if name.endswith(".xls"):
            return _read_xls(data)
    except UnsupportedFile:
        raise
    except Exception as exc:  # corrupt or mislabelled files
        raise UnsupportedFile(f"Could not read '{filename}': {exc}") from exc
    raise UnsupportedFile("Only CSV, XLSX and XLS files are supported")


def pick_sheet(sheets: list[Sheet], name: str | None = None) -> Sheet:
    if name:
        for sheet in sheets:
            if sheet.name == name:
                return sheet
        raise UnsupportedFile(f"Sheet '{name}' not found")
    for sheet in sheets:
        if any(sheet.rows):
            return sheet
    raise UnsupportedFile("The file is empty")
