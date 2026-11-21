"""Read a LinkedIn send list (.xlsx or .csv) and write per-row results back into it.

Columns used (case-insensitive): name, company, send_day, sent, message / followup_dm. Jarvis adds linkedin_status.
The first write keeps a copy of the original next to it (<file>.orig.<ext>)."""

import csv
import shutil
from pathlib import Path


def _path(file: str) -> Path:
    p = Path(file.strip()).expanduser()
    if not p.is_absolute() and not p.exists():
        for d in ("~/Downloads", "~/Desktop", "~/Documents"):
            if (q := Path(d).expanduser() / p).exists():
                return q
    return p


def read(file: str) -> tuple[Path, list[dict]]:
    """Rows as dicts with lower-case headers plus "_row" (1-based sheet row); first sheet of an .xlsx."""
    p = _path(file)
    if p.suffix.lower() == ".csv":
        with p.open(newline="", encoding="utf-8-sig") as fh:
            grid = list(csv.reader(fh))
    else:
        import openpyxl
        grid = [list(r) for r in openpyxl.load_workbook(p, read_only=True, data_only=True).worksheets[0].iter_rows(values_only=True)]
    head = [str(h or "").strip().lower() for h in grid[0]]
    rows = []
    for i, r in enumerate(grid[1:], 2):
        d = {h: ("" if v is None else str(v).strip()) for h, v in zip(head, r) if h}
        if d.get("name"):
            rows.append({**d, "_row": i})
    return p, rows


def write(p: Path, updates: dict[int, dict[str, str]]) -> None:
    """updates: {sheet row: {column: value}}; adds missing columns at the end."""
    if not updates:
        return
    orig = p.with_name(f"{p.stem}.orig{p.suffix}")
    if not orig.exists():
        shutil.copy2(p, orig)
    if p.suffix.lower() == ".csv":
        with p.open(newline="", encoding="utf-8-sig") as fh:
            grid = list(csv.reader(fh))
        for row, cols in updates.items():
            for col, val in cols.items():
                head = [h.strip().lower() for h in grid[0]]
                if col not in head:
                    grid[0].append(col)
                    head.append(col)
                c = head.index(col)
                grid[row - 1] += [""] * (c + 1 - len(grid[row - 1]))
                grid[row - 1][c] = val
        with p.open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows(grid)
        return
    import openpyxl
    wb = openpyxl.load_workbook(p)
    ws = wb.worksheets[0]
    head = [str(c.value or "").strip().lower() for c in ws[1]]
    for row, cols in updates.items():
        for col, val in cols.items():
            if col not in head:
                head.append(col)
                ws.cell(1, len(head), col)
            ws.cell(row, head.index(col) + 1, val)
    wb.save(p)
