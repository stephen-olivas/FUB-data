#!/usr/bin/env python3
"""Rebuild data/service_zips.csv from the core-markets workbook.

  pip install openpyxl
  python scripts/build_service_zips.py "ReSvcs Zips All Core Markets.xlsx"

Each sheet is a market; columns: Zip, City, County, ZHVI, Miles from <market>, [Zone].
"""

import csv
import sys
from pathlib import Path

import openpyxl

OUT = Path(__file__).resolve().parent.parent / "data" / "core_market_zips.csv"


def main(xlsx: str) -> None:
    wb = openpyxl.load_workbook(xlsx, read_only=True, data_only=True)
    rows = []
    for ws in wb.worksheets:
        it = ws.iter_rows(values_only=True)
        next(it, None)
        for r in it:
            if not r or r[0] is None:
                continue
            z = str(r[0]).strip().split(".")[0].zfill(5)
            miles = r[4] if len(r) > 4 and r[4] is not None else ""
            zone = r[5] if len(r) > 5 and r[5] else ""
            rows.append([z, ws.title, r[1] or "", r[2] or "", miles, zone])
    with OUT.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["zip", "market", "city", "county", "miles_from_center", "zone"])
        w.writerows(rows)
    print(f"wrote {len(rows)} zips to {OUT}")


if __name__ == "__main__":
    main(sys.argv[1])
