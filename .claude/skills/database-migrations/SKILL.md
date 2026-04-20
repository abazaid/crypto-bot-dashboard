---
name: database-migrations
description: Database migration best practices for SQLAlchemy + Alembic. Safe schema changes, data migrations, rollbacks, and zero-downtime deployments for the crypto-bots SQLite/PostgreSQL database.
origin: ECC
---

# Database Migration Patterns

Safe, reversible database schema changes using Alembic with SQLAlchemy.

## When to Activate

- Adding new columns or tables to the trading bot DB
- Modifying existing schema (Campaign, Position, DcaRule models)
- Running data migrations (backfill, transform existing rows)
- Setting up Alembic for the first time

## Core Principles

1. **Every change is a migration** — never alter the DB schema manually in production
2. **Migrations are forward-only** — rollbacks use new forward migrations
3. **Schema and data migrations are separate** — never mix DDL and DML
4. **Test against production-sized data** — a migration on 100 rows may lock on 100k
5. **Migrations are immutable once deployed** — never edit a migration that has run

## Setting Up Alembic (First Time)

```bash
pip install alembic
alembic init alembic

# Edit alembic.ini:
# sqlalchemy.url = sqlite:///./trading.db

# Edit alembic/env.py to import your models:
from app.models.paper_v2 import Base
target_metadata = Base.metadata
```

## Workflow

```bash
# Generate migration from model changes
alembic revision --autogenerate -m "add_circuit_breaker_column"

# Review the generated migration file before applying!

# Apply pending migrations
alembic upgrade head

# Show current version
alembic current

# Show migration history
alembic history

# Rollback one migration
alembic downgrade -1
```

## Migration Safety Checklist

Before applying any migration:

- [ ] Migration has both `upgrade()` and `downgrade()` functions
- [ ] New columns have defaults or are nullable
- [ ] No full table locks on large tables
- [ ] Data backfill is a separate migration from schema change
- [ ] Tested on a copy of the production database
- [ ] Rollback plan documented

## Adding a Column Safely

```python
# migrations/versions/001_add_circuit_breaker.py
from alembic import op
import sqlalchemy as sa

def upgrade():
    # GOOD: nullable column with default — no lock, no rewrite
    op.add_column(
        "campaigns",
        sa.Column("circuit_breaker_enabled", sa.Boolean(), nullable=False, server_default="1")
    )
    op.add_column(
        "campaigns",
        sa.Column("max_consecutive_losses", sa.Integer(), nullable=True)
    )

def downgrade():
    op.drop_column("campaigns", "max_consecutive_losses")
    op.drop_column("campaigns", "circuit_breaker_enabled")
```

## Data Migration (Backfill)

```python
# migrations/versions/002_backfill_circuit_breaker_defaults.py
from alembic import op
from sqlalchemy.sql import text

def upgrade():
    # Separate migration: backfill data after schema is in place
    conn = op.get_bind()
    conn.execute(text("""
        UPDATE campaigns
        SET max_consecutive_losses = 3
        WHERE max_consecutive_losses IS NULL
    """))

def downgrade():
    pass  # Data migration — no meaningful reverse
```

## Adding an Index

```python
def upgrade():
    # Add index for frequently queried columns
    op.create_index(
        "idx_positions_campaign_symbol",
        "positions",
        ["campaign_id", "symbol"],
    )
    op.create_index(
        "idx_activity_log_created_at",
        "activity_logs",
        ["created_at"],
    )

def downgrade():
    op.drop_index("idx_positions_campaign_symbol", table_name="positions")
    op.drop_index("idx_activity_log_created_at", table_name="activity_logs")
```

## Recommended Indexes for This Project

These indexes should be added to improve query performance:

```python
# In models/paper_v2.py, add to table definitions:
# Or as a migration:

indexes_to_add = [
    ("campaigns", ["status"]),                    # Active campaign filter
    ("positions", ["campaign_id", "symbol"]),     # Position lookup
    ("positions", ["status"]),                    # Open positions filter
    ("dca_rules", ["campaign_id"]),              # DCA rules by campaign
    ("activity_logs", ["created_at"]),            # Log cleanup + pagination
    ("market_snapshots", ["symbol", "timestamp"]), # Price history queries
]
```

## ActivityLog Cleanup Migration

```python
# migrations/versions/003_add_activitylog_retention.py
def upgrade():
    # Add index to support efficient cleanup
    op.create_index("idx_activity_log_created_at", "activity_logs", ["created_at"])

    # Optional: Create a view for recent logs only
    op.execute("""
        CREATE VIEW recent_activity AS
        SELECT * FROM activity_logs
        WHERE created_at > datetime('now', '-30 days')
    """)

def downgrade():
    op.execute("DROP VIEW IF EXISTS recent_activity")
    op.drop_index("idx_activity_log_created_at", table_name="activity_logs")
```

## Anti-Patterns

| Anti-Pattern | Why It Fails | Better Approach |
|-------------|-------------|-----------------|
| Manual SQL in production | No audit trail | Always use Alembic migrations |
| Editing deployed migrations | Causes env drift | Create new migration instead |
| NOT NULL without default | Locks table, rewrites all rows | Add nullable first, backfill, then add constraint |
| Schema + data in one migration | Hard to rollback | Separate migrations |
| No downgrade() function | Can't roll back | Always implement downgrade |
