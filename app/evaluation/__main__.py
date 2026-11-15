"""Print the extraction accuracy report: uv run python -m app.evaluation"""

import json
import sys

from app.evaluation.accuracy import evaluate

report = evaluate()
print(json.dumps(report.as_dict(), indent=2, default=str, ensure_ascii=False))
sys.exit(0 if report.passed else 1)
