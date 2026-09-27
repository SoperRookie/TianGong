"""表格类需求文件（Excel / CSV）：按工作表 → 行渲染为文本，表头作为章节标题保留。"""

import csv
import io
from pathlib import Path

from app.parsers.base import ParsedDocument, Section


class TableParser:
    suffixes = (".xlsx", ".xlsm", ".csv", ".tsv")

    def parse(self, path: Path) -> ParsedDocument:
        suffix = path.suffix.lower()
        sheets: list[tuple[str, list[list[str]]]] = []
        if suffix in (".csv", ".tsv"):
            from app.parsers.base import read_text_any

            text = read_text_any(path)
            rows = list(csv.reader(io.StringIO(text), delimiter="\t" if suffix == ".tsv" else ","))
            sheets.append((path.stem, rows))
        else:
            from openpyxl import load_workbook

            wb = load_workbook(path, read_only=True, data_only=True)
            for ws in wb.worksheets:
                rows = [[self._cell(c) for c in row] for row in ws.iter_rows(values_only=True)]
                sheets.append((ws.title, rows))
        sections: list[Section] = []
        tables: list[list[list[str]]] = []
        for name, rows in sheets:
            rows = [[str(c).strip() for c in r] for r in rows if any(str(c).strip() for c in r)]
            if not rows:
                continue
            sections.append(Section(level=1, title=name if len(sheets) > 1 or suffix not in (".csv", ".tsv") else ""))
            tables.append(rows)
        if not tables:
            raise ValueError(f"{path.name} 没有可读取的表格内容")
        return ParsedDocument(source=path.name, doc_type=suffix.lstrip("."), sections=[s for s in sections if s.title],
                              tables=tables)

    @staticmethod
    def _cell(value) -> str:
        if value is None:
            return ""
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        return str(value)
