"""Accuracy harness for record extraction (KASU-018).

Runs each labelled case through the real import pipeline (`extract_row`, the same code that
reads uploaded spreadsheets) and measures:

* critical numeric field accuracy: date, amount and quantity read exactly right.
  Target >= 98%. A field we refuse to read (an issue for the user) counts as wrong here, but
  is reported separately from *silent errors* (a wrong value with no warning), which are the
  dangerous kind.
* high-confidence classification precision: of the kinds and expense categories we're
  confident about (confidence >= REVIEW_THRESHOLD, so they aren't sent for review), how many
  are right. Target >= 95%. Coverage says how often we were confident at all.
"""

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from app.evaluation.dataset import CASES, Case
from app.services.imports.rows import REVIEW_THRESHOLD, extract_row

CRITICAL_ACCURACY_TARGET = Decimal(98)
PRECISION_TARGET = Decimal(95)

_NUMERIC = (("occurred_on", "occurred_on"), ("amount", "amount"), ("quantity", "quantity"))


def _pct(part: int, whole: int) -> Decimal | None:
    if not whole:
        return None
    return (Decimal(part) / whole * 100).quantize(Decimal("0.1"), ROUND_HALF_UP)


def _same(expected, got) -> bool:
    if expected is None or got is None:
        return expected == got
    try:
        return Decimal(str(expected)) == Decimal(str(got))
    except ArithmeticError:
        return str(expected) == str(got)


@dataclass
class _Tally:
    checked: int = 0
    correct: int = 0
    silent_errors: int = 0
    unread: int = 0


@dataclass
class _Precision:
    cases: int = 0
    confident: int = 0
    confident_correct: int = 0


@dataclass
class Report:
    cases: int
    critical: dict[str, _Tally]
    classification: dict[str, _Precision]
    failures: list[dict] = field(default_factory=list)

    @property
    def critical_accuracy_pct(self) -> Decimal | None:
        return _pct(
            sum(t.correct for t in self.critical.values()),
            sum(t.checked for t in self.critical.values()),
        )

    @property
    def precision_pct(self) -> Decimal | None:
        return _pct(
            sum(p.confident_correct for p in self.classification.values()),
            sum(p.confident for p in self.classification.values()),
        )

    @property
    def passed(self) -> bool:
        acc, prec = self.critical_accuracy_pct, self.precision_pct
        return (
            acc is not None
            and acc >= CRITICAL_ACCURACY_TARGET
            and prec is not None
            and prec >= PRECISION_TARGET
        )

    def as_dict(self) -> dict:
        return {
            "cases": self.cases,
            "critical_fields": {
                "accuracy_pct": self.critical_accuracy_pct,
                "target_pct": CRITICAL_ACCURACY_TARGET,
                "by_field": {
                    name: {
                        "checked": t.checked,
                        "correct": t.correct,
                        "accuracy_pct": _pct(t.correct, t.checked),
                        "silent_errors": t.silent_errors,
                        "unread": t.unread,
                    }
                    for name, t in self.critical.items()
                },
            },
            "classification": {
                "high_confidence_precision_pct": self.precision_pct,
                "target_pct": PRECISION_TARGET,
                "by_field": {
                    name: {
                        "cases": p.cases,
                        "confident": p.confident,
                        "coverage_pct": _pct(p.confident, p.cases),
                        "precision_pct": _pct(p.confident_correct, p.confident),
                    }
                    for name, p in self.classification.items()
                },
            },
            "passed": self.passed,
            "failures": self.failures,
        }


def _row_cells(row: dict) -> tuple[list, dict[str, int]]:
    names = list(row)
    return [row[n] for n in names], {n: i for i, n in enumerate(names)}


def evaluate(cases: list[Case] = CASES) -> Report:
    critical = {name: _Tally() for name, _ in _NUMERIC}
    classification = {"kind": _Precision(), "category": _Precision()}
    failures: list[dict] = []

    for n, case in enumerate(cases, 1):
        cells, mapping = _row_cells(case.row)
        parsed = extract_row(cells, mapping)
        conf = parsed["field_confidence"]

        for name, key in _NUMERIC:
            if name not in case.expect:
                continue
            want, got = case.expect[name], parsed.get(key)
            tally = critical[name]
            tally.checked += 1
            if _same(want, got):
                tally.correct += 1
                continue
            if got is None:
                tally.unread += 1
            else:
                tally.silent_errors += 1
            failures.append(
                {"case": n, "field": name, "row": case.row, "expected": want, "got": got}
            )

        for name in ("kind", "category"):
            if name not in case.expect:
                continue
            p = classification[name]
            p.cases += 1
            got, confidence = parsed.get(name), conf.get(name, 0.0)
            if got is None or confidence < REVIEW_THRESHOLD:
                continue  # sent to the user for review: not a confident claim
            p.confident += 1
            if got == case.expect[name]:
                p.confident_correct += 1
            else:
                failures.append(
                    {
                        "case": n,
                        "field": name,
                        "row": case.row,
                        "expected": case.expect[name],
                        "got": got,
                        "confidence": confidence,
                    }
                )

    return Report(
        cases=len(cases), critical=critical, classification=classification, failures=failures
    )
