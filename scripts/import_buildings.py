#!/usr/bin/env python3
"""Build or refresh the local SQLite/RTree index from CityGML LoD1 files."""

import argparse

from app.config import settings
from app.services.propagation.building_index import BuildingIndex


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="rebuild even when sources are unchanged")
    args = parser.parse_args()
    count = BuildingIndex(settings.buildings_index_path).build(
        settings.buildings_raw_dir, force=args.force
    )
    print(f"Indexed {count} buildings in {settings.buildings_index_path}")


if __name__ == "__main__":
    main()
