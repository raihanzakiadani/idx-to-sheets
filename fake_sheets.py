"""Google Sheets palsu (in-memory) untuk uji alur tulis/baca tanpa akses jaringan."""
import gspread


class FakeWS:
    def __init__(self, title, rows=100, cols=10):
        self.title, self.row_count, self.col_count, self.grid = title, rows, cols, []

    def resize(self, rows=None, cols=None):
        self.row_count = rows or self.row_count
        self.col_count = cols or self.col_count

    def clear(self):
        self.grid = []

    def _set(self, r, c, v):
        while len(self.grid) <= r:
            self.grid.append([])
        row = self.grid[r]
        while len(row) <= c:
            row.append("")
        row[c] = "" if v is None else v

    def update(self, values, range_name="A1", value_input_option=None):
        import re
        m = re.match(r"([A-Z]+)(\d+)", range_name)
        r0 = int(m.group(2)) - 1
        assert r0 + len(values) <= self.row_count, f"{self.title}: update di luar grid ({r0 + len(values)} > {self.row_count})"
        for i, row in enumerate(values):
            for j, v in enumerate(row):
                self._set(r0 + i, j, v)

    def append_rows(self, rows, value_input_option=None, table_range=None):
        r0 = len(self.grid)
        self.row_count = max(self.row_count, r0 + len(rows))
        for i, row in enumerate(rows):
            for j, v in enumerate(row):
                self._set(r0 + i, j, v)

    def row_values(self, n):
        return list(self.grid[n - 1]) if len(self.grid) >= n else []

    def col_values(self, n):
        return [str(r[n - 1]) if len(r) >= n else "" for r in self.grid]

    def get_all_values(self):
        w = max((len(r) for r in self.grid), default=0)
        return [[str(c) for c in r + [""] * (w - len(r))] for r in self.grid]


class FakeSpreadsheet:
    def __init__(self):
        self.sheets = {}

    def worksheet(self, title):
        if title not in self.sheets:
            raise gspread.exceptions.WorksheetNotFound(title)
        return self.sheets[title]

    def add_worksheet(self, title, rows, cols):
        self.sheets[title] = FakeWS(title, rows, cols)
        return self.sheets[title]
