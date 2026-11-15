"""Generate and deliver last week's brief for every active business (KASU-030).

Run once a week, after the week ends, e.g. every Monday at 06:00 Lagos time:

    0 6 * * 1  cd /srv/kasu && uv run python -m app.jobs.weekly_briefs

Safe to re-run: a business never gets the same week's brief twice. Exits non-zero if any
business failed, so the scheduler can alert.
"""

import argparse
import json
import logging
import sys
from dataclasses import asdict
from datetime import date

from app.core.db import SessionLocal
from app.services.briefs import run_weekly_briefs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--today",
        type=date.fromisoformat,
        help="Pretend today is this date (YYYY-MM-DD); briefs cover the week before it.",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    with SessionLocal() as db:
        summary = run_weekly_briefs(db, today=args.today)
    print(json.dumps(asdict(summary), default=str))
    return 1 if summary.failed else 0


if __name__ == "__main__":
    sys.exit(main())
