"""Persist test membership and interrupted resets; fence writes during cleanup.

Revision ID: 0105
Revises: 0104
"""

import sqlalchemy as sa
from alembic import op

revision = '0105'
down_revision = '0104'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '2s'")
    op.execute("SET LOCAL statement_timeout = '20s'")
    for column in (
        sa.Column('test_account_enabled', sa.Boolean(), nullable=True),
        sa.Column('test_reset_state', sa.String(20), nullable=True),
        sa.Column('test_reset_started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('test_reset_completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('test_reset_panel_uuids', sa.JSON(), nullable=True),
    ):
        op.add_column('users', column)

    install_guards()


def install_guards() -> None:
    # A callback/worker which was already running before the reset must not
    # write through its in-memory User snapshot. Only the reset transaction
    # sets the local bypass. Provider callbacks fail and can be retried.
    op.execute("""
    CREATE FUNCTION teplo_test_reset_write_guard() RETURNS trigger AS $$
    DECLARE owner_id integer; reset_state text; row_data jsonb;
    BEGIN
      IF current_setting('app.test_account_reset', true) = 'on' THEN
        IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
      END IF;
      IF TG_OP = 'DELETE' THEN row_data := to_jsonb(OLD); ELSE row_data := to_jsonb(NEW); END IF;
      IF TG_TABLE_NAME = 'users' THEN
        IF OLD.test_reset_state IN ('resetting', 'failed') THEN
          RAISE EXCEPTION 'test_account_reset_in_progress' USING ERRCODE = '55000';
        END IF;
        IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
      END IF;
      owner_id := (row_data ->> 'user_id')::integer;
      IF owner_id IS NULL AND row_data ? 'subscription_id' THEN
        SELECT user_id INTO owner_id FROM subscriptions WHERE id = (row_data ->> 'subscription_id')::integer;
      END IF;
      IF owner_id IS NULL AND row_data ? 'checkout_id' THEN
        SELECT user_id INTO owner_id FROM subscription_checkouts WHERE id = (row_data ->> 'checkout_id')::integer;
      END IF;
      -- Ordinary clients never acquire this additional row lock.
      IF NOT EXISTS (SELECT 1 FROM users WHERE id = owner_id AND test_reset_state IS NOT NULL) THEN
        IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
      END IF;
      SELECT test_reset_state INTO reset_state FROM users WHERE id = owner_id FOR SHARE;
      IF reset_state IN ('resetting', 'failed') THEN
        RAISE EXCEPTION 'test_account_reset_in_progress' USING ERRCODE = '55000';
      END IF;
      IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END;
    $$ LANGUAGE plpgsql;
    """)
    op.execute("""
    CREATE TRIGGER teplo_test_reset_guard BEFORE UPDATE OR DELETE ON users
      FOR EACH ROW EXECUTE FUNCTION teplo_test_reset_write_guard();
    """)
    # Install on owner rows, not referred_by_id/referral_id: invited clients
    # and their money must remain usable while their referrer's stand resets.
    op.execute("""
    DO $$ DECLARE target record; BEGIN
      FOR target IN
        SELECT DISTINCT c.conrelid::regclass AS relation
        FROM pg_constraint c JOIN pg_attribute a
          ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
        WHERE c.contype = 'f' AND array_length(c.conkey, 1) = 1
          AND ((c.confrelid = 'users'::regclass AND a.attname = 'user_id')
            OR (c.confrelid = 'subscriptions'::regclass AND a.attname = 'subscription_id')
            OR (c.confrelid = 'subscription_checkouts'::regclass AND a.attname = 'checkout_id'))
          AND c.conrelid <> 'admin_audit_log'::regclass
      LOOP
        EXECUTE format('CREATE TRIGGER teplo_test_reset_guard BEFORE INSERT OR UPDATE OR DELETE ON %s '
          'FOR EACH ROW EXECUTE FUNCTION teplo_test_reset_write_guard()', target.relation);
      END LOOP;
    END $$;
    """)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '2s'")
    op.execute("SET LOCAL statement_timeout = '20s'")
    if op.get_bind().scalar(
        sa.text(
            'SELECT EXISTS(SELECT 1 FROM users WHERE test_reset_state IS NOT NULL OR test_account_enabled IS NOT NULL OR test_reset_started_at IS NOT NULL OR test_reset_completed_at IS NOT NULL OR test_reset_panel_uuids IS NOT NULL)'
        )
    ):
        raise RuntimeError('Test-reset history/membership exists; retain additive schema on code rollback')
    op.execute("""
    DO $$ DECLARE target record; BEGIN
      FOR target IN SELECT tgrelid::regclass AS relation FROM pg_trigger
        WHERE tgname = 'teplo_test_reset_guard'
      LOOP EXECUTE format('DROP TRIGGER teplo_test_reset_guard ON %s', target.relation); END LOOP;
    END $$;
    """)
    op.execute('DROP FUNCTION teplo_test_reset_write_guard()')
    for name in (
        'test_reset_panel_uuids',
        'test_reset_completed_at',
        'test_reset_started_at',
        'test_reset_state',
        'test_account_enabled',
    ):
        op.drop_column('users', name)
