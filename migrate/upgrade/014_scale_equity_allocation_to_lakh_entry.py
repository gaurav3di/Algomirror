"""
Migration: rescale equity_fund_allocation from a lakh-shaped entry to rupees

The Accounts screen originally labelled Equity Fund Allocation as rupees but was
filled in as lakh, so a 40 lakh corpus was stored as the number 40. The ratio
column still looked correct, because a ratio is scale invariant and the unit
cancels out, but Invested percent and Stake percent are money divided by
allocation and so came out roughly 100000 times too large: a real screen showed
Invested percent of 258620 against a 40 lakh corpus.

The screen now takes the figure in lakh explicitly and converts to rupees before
it reaches the API, so the stored column is rupees from here on. This migration
brings the already-saved rows onto that footing.

Idempotency. The runner records applied migrations in applied_migrations and will
not call this twice, but the guard below makes a manual re-run harmless as well:
only rows that are positive and below one lakh are touched. A genuine rupee
corpus is far above that threshold, so a row that has already been converted is
left alone. A row of exactly zero is also left alone, since zero scaled by
anything is still zero and the account is simply unallocated.
"""

from sqlalchemy import text

# A stored value below this is read as a lakh-shaped entry rather than rupees.
# One lakh is the smallest corpus that is plausible as a real rupee figure, and
# any lakh-shaped entry (40, 15, 65) sits far below it.
LAKH = 100000
CONVERSION_THRESHOLD = LAKH


def upgrade(db):
    """Multiply lakh-shaped equity allocations up to rupees."""

    # The equity module may not be installed yet on a database that has not run
    # migration 013. Nothing to do in that case.
    inspector_sql = text("SELECT 1 FROM equity_account_allocations LIMIT 1")
    try:
        db.session.execute(inspector_sql)
    except Exception:
        db.session.rollback()
        print("  equity_account_allocations not present, skipping (run 013 first)")
        return

    select_sql = text(
        "SELECT id, account_id, equity_fund_allocation "
        "FROM equity_account_allocations "
        "WHERE equity_fund_allocation > 0 AND equity_fund_allocation < :threshold"
    )
    rows = db.session.execute(select_sql, {"threshold": CONVERSION_THRESHOLD}).fetchall()

    if not rows:
        print("  No lakh-shaped equity allocations found, nothing to rescale")
        return

    update_sql = text(
        "UPDATE equity_account_allocations "
        "SET equity_fund_allocation = equity_fund_allocation * :factor "
        "WHERE id = :row_id"
    )

    for row in rows:
        row_id, account_id, before = row[0], row[1], float(row[2])
        db.session.execute(update_sql, {"factor": LAKH, "row_id": row_id})
        print(
            "  account %s: %s -> %s"
            % (account_id, format(before, ".2f"), format(before * LAKH, ".2f"))
        )

    db.session.commit()
    print("  Rescaled %d equity allocation row(s) from lakh to rupees" % len(rows))


def downgrade(db):
    """Divide rupee allocations back down to the lakh-shaped entry."""
    select_sql = text(
        "SELECT id FROM equity_account_allocations "
        "WHERE equity_fund_allocation >= :threshold"
    )
    try:
        rows = db.session.execute(select_sql, {"threshold": CONVERSION_THRESHOLD}).fetchall()
    except Exception:
        db.session.rollback()
        print("  equity_account_allocations not present, nothing to undo")
        return

    update_sql = text(
        "UPDATE equity_account_allocations "
        "SET equity_fund_allocation = equity_fund_allocation / :factor "
        "WHERE id = :row_id"
    )
    for row in rows:
        db.session.execute(update_sql, {"factor": LAKH, "row_id": row[0]})

    db.session.commit()
    print("  Reverted %d equity allocation row(s) to the lakh-shaped entry" % len(rows))
