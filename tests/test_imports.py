import io
from datetime import datetime

from openpyxl import Workbook

CSV = b"""Date,Item,Qty,Amount,Type,Customer
01/10/2026,Rice,2,"320,000",purchase,
03/10/2026,Rice,1,200000,sale,Mrs Bello
04/10/2026,Shop rent,,50000,expense,
05/10/2026,Something,,abc,sale,
06/10/2026,keke to market,,2000,,
,,,,,
Total,,,572000,,
"""


def _upload(api, data: bytes, name="records.csv", **form):
    return api.post("/imports", files={"file": (name, data)}, data=form)


def _by_number(batch):
    return {r["row_number"]: r for r in batch["rows"]}


def test_csv_import_stages_rows_for_review(api):
    res = _upload(api, CSV)
    assert res.status_code == 201, res.text
    batch = res.json()
    assert batch["status"] == "needs_review"
    assert batch["column_mapping"] == {
        "date": 0,
        "product": 1,
        "quantity": 2,
        "amount": 3,
        "kind": 4,
        "customer": 5,
    }
    rows = _by_number(batch)
    assert set(rows) == {2, 3, 4, 5, 6, 8}  # blank line 7 dropped; numbers match the file

    assert rows[2]["status"] == "ready" and rows[2]["parsed"]["kind"] == "purchase"
    assert rows[2]["parsed"]["amount"] == "320000.00"
    assert rows[3]["status"] == "ready" and rows[3]["parsed"]["occurred_on"] == "2026-10-03"
    assert rows[4]["status"] == "ready" and rows[4]["parsed"]["category"] == "rent"

    # Bad amount -> blocking issue, not a guess.
    assert rows[5]["status"] == "needs_review"
    assert any(i["field"] == "amount" and i["blocking"] for i in rows[5]["issues"])
    # No type given: guessed expense/transport, but flagged for confirmation.
    assert rows[6]["status"] == "needs_review"
    assert rows[6]["parsed"]["kind"] == "expense"
    assert rows[6]["parsed"]["category"] == "transport"
    assert not any(i["blocking"] for i in rows[6]["issues"])
    # Totals row is never imported as a transaction.
    assert any(i["blocking"] for i in rows[8]["issues"])

    # Nothing is in the ledger until commit.
    assert api.get("/transactions").json()["total"] == 0


def test_fix_row_then_commit(api):
    batch = _upload(api, CSV).json()
    rows = _by_number(batch)

    fixed = api.patch(
        f"/imports/{batch['id']}/rows/{rows[5]['id']}", json={"changes": {"amount": "₦15,000"}}
    )
    assert fixed.status_code == 200, fixed.text
    assert fixed.json()["status"] == "ready"
    assert fixed.json()["parsed"]["amount"] == "15000.00"

    bad = api.patch(
        f"/imports/{batch['id']}/rows/{rows[5]['id']}", json={"changes": {"kind": "gift"}}
    )
    assert bad.status_code == 422

    committed = api.post(f"/imports/{batch['id']}/commit").json()
    assert committed["status"] == "committed"
    statuses = {r["row_number"]: r["status"] for r in committed["rows"]}
    assert statuses == {
        2: "committed",
        3: "committed",
        4: "committed",
        5: "committed",
        6: "committed",
        8: "unresolved",
    }

    txns = api.get("/transactions").json()["items"]
    assert len(txns) == 5
    by_status = {t["description"] or t["product_name"]: t["status"] for t in txns}
    assert by_status["keke to market"] == "provisional"  # unconfirmed guess
    assert by_status["Rice"] == "confirmed"
    assert all(t["source"] == "import" for t in txns)

    # Committing twice is refused.
    assert api.post(f"/imports/{batch['id']}/commit").status_code == 409


def test_reupload_detects_duplicates(api):
    first = _upload(api, CSV).json()
    api.post(f"/imports/{first['id']}/commit")

    second = _upload(api, CSV).json()
    assert second["stats"]["previously_imported_batch_id"] == first["id"]
    rows = _by_number(second)
    for n in (2, 3, 4, 6):
        assert rows[n]["status"] == "duplicate", (n, rows[n])

    api.post(f"/imports/{second['id']}/commit")
    assert api.get("/transactions").json()["total"] == 4  # nothing doubled


def test_xlsx_with_title_rows_and_in_out_columns(api):
    wb = Workbook()
    ws = wb.active
    ws.append(["Mama Nkechi shop records"])
    ws.append([])
    ws.append(["Date", "Details", "Sales", "Expenses"])
    ws.append([datetime(2026, 10, 7), "Sold rice", 15000, None])
    ws.append([datetime(2026, 10, 7), "Fuel for generator", None, 3000])
    buf = io.BytesIO()
    wb.save(buf)

    batch = _upload(api, buf.getvalue(), name="shop.xlsx").json()
    assert batch["header_row"] == 2
    rows = _by_number(batch)
    assert rows[4]["parsed"]["kind"] == "sale" and rows[4]["status"] == "ready"
    assert rows[5]["parsed"]["kind"] == "expense"
    assert rows[5]["parsed"]["category"] == "fuel"
    assert rows[5]["status"] == "ready"


def test_remap_when_headers_not_found(api):
    data = b"07/10/2026,Rice,5000\n08/10/2026,Beans,7000\n"
    batch = _upload(api, data).json()
    assert batch["error"]  # we couldn't tell what the columns are

    res = api.put(
        f"/imports/{batch['id']}/mapping",
        json={"column_mapping": {"date": 0, "product": 1, "amount": 2}, "default_kind": "sale"},
    )
    assert res.status_code == 200, res.text
    remapped = res.json()
    assert remapped["error"] is None
    assert [r["status"] for r in remapped["rows"]] == ["ready", "ready"]
    assert remapped["rows"][0]["row_number"] == 1


def test_rejects_unsupported_files(api):
    res = _upload(api, b"%PDF-1.4", name="statement.pdf")
    assert res.status_code == 422
