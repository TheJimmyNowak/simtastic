#!/usr/bin/env python3
"""Indeks budynków LoD1: lista pobranych powiatów albo ręczne (ponowne) pobranie powiatu.

Precompute pobiera brakujące powiaty sam; skrypt służy do podglądu indeksu, odświeżenia
powiatu po nowym wydaniu danych GUGiK i do wcześniejszego pobrania przed pracą offline.

    PYTHONPATH=backend python scripts/import_buildings.py            # lista powiatów
    PYTHONPATH=backend python scripts/import_buildings.py 1863 1816  # pobierz / odśwież
"""

import argparse
import sqlite3

from app.config import settings
from app.services.propagation.building_index import BuildingIndex
from app.services.propagation.gugik import BuildingProvider


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("districts", nargs="*", help="kody TERYT powiatów (4 cyfry), np. 1863")
    args = parser.parse_args()
    index = BuildingIndex(settings.buildings_index_path)
    if args.districts:
        provider = BuildingProvider(index)
        with index.lock():
            for code in args.districts:
                provider._import(code)
    index.districts()  # tworzy pusty indeks, jeśli go nie ma
    with sqlite3.connect(index.db_path) as db:
        rows = db.execute("SELECT code, year, building_count, indexed_at FROM districts ORDER BY code").fetchall()
    for code, year, count, indexed_at in rows:
        print(f"{code}  LoD1 {year}  {count:>7} budynków  pobrany {indexed_at[:19]}")
    print(f"{len(rows)} powiatów w {index.db_path}")


if __name__ == "__main__":
    main()
