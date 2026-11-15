"""Print platform-wide data quality for the last N days (KASU-018).

uv run python -m app.jobs.data_quality --days 30
"""

import argparse
import json
from datetime import date, timedelta

from app.core.db import SessionLocal
from app.services.data_quality import data_quality


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args(argv)
    end = date.today()
    with SessionLocal() as db:
        report = data_quality(db, None, end - timedelta(days=args.days - 1), end)
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
