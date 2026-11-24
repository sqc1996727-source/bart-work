"""Strict stdlib-only CSV and XLSX parsing for complete V9 waybill rows."""
from __future__ import annotations

import csv
import datetime as dt
import io
import math
import re
import zipfile
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import PurePosixPath
from xml.etree import ElementTree as ET

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_ROWS = 5000
MAX_COLUMNS = 32
MAX_CELL_CHARS = 4000
MAX_XLSX_UNCOMPRESSED = 40 * 1024 * 1024
MAX_ZIP_MEMBERS = 1000

CANONICAL_HEADERS = {
    "number": {"number", "waybillnumber", "waybillid", "运单编号", "运单单号", "运单号", "厂家合同编号", "厂家合同运单编号", "合同编号", "单号"},
    "date": {"date", "shipdate", "dispatchdate", "发货日期", "发运日期"},
    "receiver": {"receiver", "recipient", "收货人", "收件人", "收货人姓名"},
    "phone": {"phone", "recipientphone", "contactphone", "联系电话", "收货人电话", "电话"},
    "address": {"address", "recipientaddress", "收货地址", "完整收货地址"},
    "destination": {"destination", "station", "到站", "目的地"},
    "cargo": {"cargo", "goods", "货物", "货物名称"},
    "qty": {"qty", "quantity", "数量", "件数"},
    "weight": {"weight", "重量", "重量吨", "重量吨数"},
    "amount": {"amount", "freight", "freighttotal", "运费", "运费元", "厂家运费", "厂家运费总额", "厂家运费总额元", "运费金额"},
    "carrier": {"carrier", "partner", "承运方", "中转方", "承运中转方", "承运单位"},
    "remark": {"remark", "note", "备注", "说明"},
    "feeWarehouse": {"feewarehouse", "warehousefee", "进仓费", "进仓费元", "仓储费"},
}
REQUIRED_FIELDS = (
    "number", "date", "receiver", "phone", "address", "destination",
    "cargo", "qty", "weight", "amount", "carrier",
)
TEXT_LIMITS = {
    "number": 80, "receiver": 120, "phone": 80, "address": 500,
    "destination": 120, "cargo": 200, "qty": 80, "carrier": 160,
    "remark": 1000,
}


class ImportValidationError(ValueError):
    def __init__(self, errors: list[dict]):
        super().__init__("invalid_import")
        self.errors = errors


def _header_key(value: str) -> str:
    return re.sub(r"[\s_\-./（）()\[\]：:]+", "", str(value)).casefold()


_HEADER_LOOKUP = {
    _header_key(alias): field
    for field, aliases in CANONICAL_HEADERS.items()
    for alias in aliases
}


def _mime_type(content_type: str) -> str:
    return str(content_type or "").split(";", 1)[0].strip().lower()


def parse_workbook(filename: str, content_type: str, data: bytes) -> list[dict]:
    """Return canonical rows with source row numbers or raise field-safe errors."""
    name = str(filename or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    mime = _mime_type(content_type)
    if not isinstance(data, bytes) or not data:
        raise ImportValidationError([{"code": "empty_file"}])
    if len(data) > MAX_FILE_BYTES:
        raise ImportValidationError([{"code": "file_too_large", "max_bytes": MAX_FILE_BYTES}])
    if extension == "csv" and mime in {"text/csv", "application/csv", "application/vnd.ms-excel", "application/octet-stream", ""}:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ImportValidationError([{"code": "unsupported_encoding", "encoding": "UTF-8"}]) from None
        if "\x00" in text:
            raise ImportValidationError([{"code": "invalid_text_file"}])
        records = _csv_records(text)
    elif extension == "xlsx" and mime in {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/octet-stream", "",
    }:
        records = _xlsx_records(data)
    else:
        raise ImportValidationError([{"code": "unsupported_file_type", "allowed": [".csv", ".xlsx"]}])
    return _canonicalize_records(records)


def _csv_records(text: str) -> list[tuple[int, list[str]]]:
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text, newline=""), dialect)
    rows = []
    for row_number, row in enumerate(reader, start=1):
        if len(row) > MAX_COLUMNS:
            raise ImportValidationError([{"code": "too_many_columns", "row": row_number}])
        if any(len(str(cell)) > MAX_CELL_CHARS for cell in row):
            raise ImportValidationError([{"code": "cell_too_long", "row": row_number}])
        if any("\n" in str(cell) or "\r" in str(cell) for cell in row):
            raise ImportValidationError([{"code": "multiline_cell_unsupported", "row": row_number}])
        if not row or all(not str(cell).strip() for cell in row):
            continue
        rows.append((row_number, [str(cell).strip() for cell in row]))
        if len(rows) > MAX_ROWS + 1:
            raise ImportValidationError([{"code": "too_many_rows", "max_rows": MAX_ROWS}])
    return rows


def _safe_xml(archive: zipfile.ZipFile, name: str) -> ET.Element:
    try:
        raw = archive.read(name)
    except KeyError:
        raise ImportValidationError([{"code": "invalid_xlsx_structure"}]) from None
    upper = raw.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ImportValidationError([{"code": "unsafe_xlsx_xml"}])
    try:
        return ET.fromstring(raw)
    except ET.ParseError:
        raise ImportValidationError([{"code": "invalid_xlsx_xml"}]) from None


def _xlsx_records(data: bytes) -> list[tuple[int, list[str]]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError):
        raise ImportValidationError([{"code": "invalid_xlsx_archive"}]) from None
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ZIP_MEMBERS or sum(x.file_size for x in infos) > MAX_XLSX_UNCOMPRESSED:
            raise ImportValidationError([{"code": "xlsx_expansion_limit"}])
        names = [item.filename for item in infos]
        if len(names) != len(set(names)):
            raise ImportValidationError([{"code": "duplicate_xlsx_member"}])
        if any(x.flag_bits & 0x1 for x in infos):
            raise ImportValidationError([{"code": "encrypted_xlsx_unsupported"}])
        for info in infos:
            path = PurePosixPath(info.filename)
            if path.is_absolute() or ".." in path.parts or "\x00" in info.filename:
                raise ImportValidationError([{"code": "unsafe_xlsx_path"}])
        if "xl/vbaProject.bin" in archive.namelist():
            raise ImportValidationError([{"code": "macro_workbook_unsupported"}])

        ns = {
            "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
            "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
            "pkg": "http://schemas.openxmlformats.org/package/2006/relationships",
        }
        workbook = _safe_xml(archive, "xl/workbook.xml")
        rels = _safe_xml(archive, "xl/_rels/workbook.xml.rels")
        rel_targets = {item.attrib.get("Id"): item.attrib.get("Target", "")
                       for item in rels.findall("pkg:Relationship", ns)}
        sheet = workbook.find("main:sheets/main:sheet", ns)
        if sheet is None:
            raise ImportValidationError([{"code": "missing_worksheet"}])
        rel_id = sheet.attrib.get("{" + ns["rel"] + "}id")
        target = rel_targets.get(rel_id, "")
        if target.startswith("/") or ".." in PurePosixPath(target).parts:
            raise ImportValidationError([{"code": "unsafe_xlsx_path"}])
        sheet_path = "xl/" + target.lstrip("/") if not target.startswith("xl/") else target
        if not sheet_path.startswith("xl/worksheets/"):
            raise ImportValidationError([{"code": "unsafe_xlsx_path"}])

        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = _safe_xml(archive, "xl/sharedStrings.xml")
            shared = ["".join(t.text or "" for t in item.findall(".//main:t", ns))
                      for item in root.findall("main:si", ns)]
        date_styles = _xlsx_date_style_indexes(archive, ns)
        zero_widths = _xlsx_leading_zero_widths(archive, ns)
        root = _safe_xml(archive, sheet_path)
        records = []
        for fallback_row_index, row in enumerate(root.findall(".//main:sheetData/main:row", ns), start=1):
            try:
                row_index = int(row.attrib.get("r", fallback_row_index))
                if row_index < 1:
                    raise ValueError
            except ValueError:
                raise ImportValidationError([{"code": "invalid_xlsx_row_number"}]) from None
            cells = []
            for cell in row.findall("main:c", ns):
                ref = cell.attrib.get("r", "")
                col = _column_number(ref)
                while len(cells) <= col:
                    cells.append("")
                if cell.find("main:f", ns) is not None:
                    raise ImportValidationError([{"code": "formula_cells_unsupported", "row": row_index}])
                value = cell.find("main:v", ns)
                cell_type = cell.attrib.get("t")
                if cell_type == "inlineStr":
                    value_text = "".join(t.text or "" for t in cell.findall(".//main:t", ns))
                elif value is None:
                    value_text = ""
                elif cell_type == "s":
                    try:
                        value_text = shared[int(value.text or "0")]
                    except (ValueError, IndexError):
                        raise ImportValidationError([{"code": "invalid_shared_string", "row": row_index}]) from None
                else:
                    value_text = value.text or ""
                    try:
                        style_index = int(cell.attrib.get("s", "0") or 0)
                    except ValueError:
                        raise ImportValidationError([{"code": "invalid_xlsx_style", "row": row_index}]) from None
                    if style_index in date_styles:
                        try:
                            serial = float(value_text)
                            if not math.isfinite(serial):
                                raise ValueError
                            value_text = (dt.datetime(1899, 12, 30) + dt.timedelta(days=serial)).date().isoformat()
                        except (ValueError, OverflowError):
                            raise ImportValidationError([{"code": "invalid_excel_date", "row": row_index}]) from None
                    elif style_index in zero_widths:
                        try:
                            numeric = float(value_text)
                            if not math.isfinite(numeric) or not numeric.is_integer():
                                raise ValueError
                            value_text = str(int(numeric)).zfill(zero_widths[style_index])
                        except ValueError:
                            raise ImportValidationError([{"code": "invalid_formatted_identifier", "row": row_index}]) from None
                if len(value_text) > MAX_CELL_CHARS:
                    raise ImportValidationError([{"code": "cell_too_long", "row": row_index}])
                cells[col] = value_text.strip()
            if any(str(x).strip() for x in cells):
                records.append((row_index, cells))
                if len(records) > MAX_ROWS + 1:
                    raise ImportValidationError([{"code": "too_many_rows", "max_rows": MAX_ROWS}])
        return records


def _xlsx_date_style_indexes(archive: zipfile.ZipFile, ns: dict[str, str]) -> set[int]:
    if "xl/styles.xml" not in archive.namelist():
        return set()
    root = _safe_xml(archive, "xl/styles.xml")
    custom = {}
    num_fmts = root.find("main:numFmts", ns)
    if num_fmts is not None:
        for item in num_fmts.findall("main:numFmt", ns):
            fmt = item.attrib.get("formatCode", "")
            if re.search(r"(?:yy|dd|hh|ss|m{1,4}[/\-])", fmt, re.I) or re.search(r"[ymd]{1,4}", fmt, re.I):
                try:
                    custom[int(item.attrib["numFmtId"])] = True
                except (KeyError, ValueError):
                    continue
    builtin_dates = set(range(14, 23)) | {45, 46, 47}
    xfs = root.find("main:cellXfs", ns)
    if xfs is None:
        return set()
    return {index for index, xf in enumerate(xfs.findall("main:xf", ns))
            if int(xf.attrib.get("numFmtId", "0")) in builtin_dates
            or custom.get(int(xf.attrib.get("numFmtId", "0")), False)}


def _xlsx_leading_zero_widths(archive: zipfile.ZipFile, ns: dict[str, str]) -> dict[int, int]:
    if "xl/styles.xml" not in archive.namelist():
        return {}
    root = _safe_xml(archive, "xl/styles.xml")
    formats = {}
    num_fmts = root.find("main:numFmts", ns)
    if num_fmts is not None:
        for item in num_fmts.findall("main:numFmt", ns):
            fmt = item.attrib.get("formatCode", "")
            if re.fullmatch(r"0{2,20}", fmt):
                try:
                    formats[int(item.attrib["numFmtId"])] = len(fmt)
                except (KeyError, ValueError):
                    continue
    xfs = root.find("main:cellXfs", ns)
    if xfs is None:
        return {}
    return {index: formats[int(xf.attrib["numFmtId"])]
            for index, xf in enumerate(xfs.findall("main:xf", ns))
            if int(xf.attrib.get("numFmtId", "0")) in formats}


def _column_number(reference: str) -> int:
    letters = "".join(c for c in reference if c.isalpha()).upper()
    result = 0
    for char in letters:
        result = result * 26 + ord(char) - ord("A") + 1
    if not letters or result > MAX_COLUMNS:
        raise ImportValidationError([{"code": "too_many_columns"}])
    return result - 1


def _canonicalize_records(records: list[tuple[int, list[str]]]) -> list[dict]:
    if len(records) < 2:
        raise ImportValidationError([{"code": "missing_header_or_rows"}])
    header_row, headers = records[0]
    fields, header_errors = [], []
    seen = set()
    for col, header in enumerate(headers, start=1):
        if not header:
            fields.append(None)
            continue
        field = _HEADER_LOOKUP.get(_header_key(header))
        if field is None:
            header_errors.append({"code": "unknown_column", "row": header_row, "column": col})
            fields.append(None)
        elif field in seen:
            header_errors.append({"code": "duplicate_column", "row": header_row, "field": field})
            fields.append(None)
        else:
            fields.append(field)
            seen.add(field)
    missing = sorted(set(REQUIRED_FIELDS) - seen)
    header_errors.extend({"code": "missing_column", "field": field} for field in missing)
    if header_errors:
        raise ImportValidationError(header_errors)

    rows, errors, seen_numbers = [], [], set()
    for row_number, values in records[1:]:
        if len(values) > len(fields):
            errors.append({"code": "too_many_cells", "row": row_number})
            continue
        source = {field: values[index].strip() if index < len(values) and field else ""
                  for index, field in enumerate(fields) if field}
        row = {"source_row": row_number}
        for field in REQUIRED_FIELDS:
            value = source.get(field, "").strip()
            if not value:
                errors.append({"code": "required_value_missing", "row": row_number, "field": field})
            elif field in TEXT_LIMITS and len(value) > TEXT_LIMITS[field]:
                errors.append({"code": "value_too_long", "row": row_number, "field": field})
            if field in TEXT_LIMITS:
                row[field] = value
        date_text = source.get("date", "").strip()
        normalized_date = _date_value(date_text)
        if date_text and normalized_date is None:
            errors.append({"code": "invalid_date", "row": row_number, "field": "date"})
        if normalized_date:
            row["date"] = normalized_date
        for field in ("amount", "feeWarehouse"):
            if field not in source or not source.get(field, "").strip():
                if field == "amount":
                    errors.append({"code": "required_value_missing", "row": row_number, "field": field})
                continue
            parsed = _money(source[field], row_number, field, errors)
            if parsed is not None:
                row[field] = parsed
        weight_text = source.get("weight", "").strip()
        try:
            weight = Decimal(weight_text)
            if not weight.is_finite() or weight <= 0:
                raise InvalidOperation
            row["weight"] = float(weight)
        except (InvalidOperation, ValueError):
            if weight_text:
                errors.append({"code": "invalid_number", "row": row_number, "field": "weight"})
        remark = source.get("remark", "").strip()
        if len(remark) > TEXT_LIMITS["remark"]:
            errors.append({"code": "value_too_long", "row": row_number, "field": "remark"})
        row["remark"] = remark
        number = source.get("number", "").strip()
        if number:
            if number in seen_numbers:
                errors.append({"code": "duplicate_waybill_in_file", "row": row_number, "field": "number"})
            seen_numbers.add(number)
        rows.append(row)
    if not rows:
        errors.append({"code": "no_data_rows"})
    if errors:
        raise ImportValidationError(errors)
    return rows


def _date_value(value: str) -> str | None:
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return dt.datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _money(value: str, row: int, field: str, errors: list[dict]) -> str | None:
    try:
        parsed = Decimal(value.strip().replace(",", "").replace("￥", "").replace("¥", ""))
        if not parsed.is_finite() or parsed < 0 or parsed.as_tuple().exponent < -2:
            raise InvalidOperation
        return str(parsed.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        errors.append({"code": "invalid_money", "row": row, "field": field})
        return None
