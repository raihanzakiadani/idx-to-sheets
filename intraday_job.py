"""Entrypoint terpisah untuk workflow intraday (tidak butuh curl_cffi / FlareSolverr)."""
import sys

from intraday import run_intraday
from sheets_util import connect_sheet

if __name__ == "__main__":
    try:
        run_intraday(connect_sheet())
    except Exception as e:  # noqa
        print(f"Intraday failed: {e}", file=sys.stderr)
        raise
