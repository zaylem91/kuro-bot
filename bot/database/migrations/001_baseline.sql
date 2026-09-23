CREATE TABLE IF NOT EXISTS mangas (
    id              SERIAL PRIMARY KEY,
    manga_name      TEXT NOT NULL UNIQUE,
    drive_folder_id TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'ongoing' CHECK (status IN ('ongoing','finished','paused','completed','dropped')),
    start_chapter   INTEGER NOT NULL DEFAULT 0,
    final_subfolder_name TEXT DEFAULT 'Final',
    last_synced_at  TIMESTAMPTZ,
    discord_channel_id TEXT,
    webhook_id TEXT,
    webhook_token TEXT,
    webhook_message_id TEXT,
    cover_url TEXT,
    mangadex_url TEXT
);

CREATE TABLE IF NOT EXISTS users (
    discord_id            TEXT PRIMARY KEY,
    finance_channel_id    TEXT,
    timezone              TEXT,
    default_role          TEXT CHECK (default_role IN ('TL', 'PR', 'ED')),
    payment_method        TEXT CHECK (payment_method IN ('binance', 'paypal')),
    payment_info          TEXT,
    drive_email           TEXT,
    monthly_period        TEXT,
    is_registered         BOOLEAN NOT NULL DEFAULT FALSE,
    can_manage_payments   BOOLEAN NOT NULL DEFAULT FALSE,
    registered_at         TIMESTAMPTZ
);
-- Migration: add drive_email for Drive folder sharing (Gmail)
ALTER TABLE users ADD COLUMN IF NOT EXISTS drive_email TEXT;
CREATE INDEX IF NOT EXISTS idx_users_drive_email ON users(LOWER(drive_email)) WHERE drive_email IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_users_drive_email_registered ON users (LOWER(drive_email)) WHERE drive_email IS NOT NULL AND is_registered = TRUE;

CREATE TABLE IF NOT EXISTS manga_staff (
    id          SERIAL PRIMARY KEY,
    manga_id    INTEGER NOT NULL REFERENCES mangas(id) ON DELETE CASCADE,
    role        TEXT NOT NULL CHECK (role IN ('TL', 'CO-TL', 'PR', 'CO-PR', 'ED', 'CO-ED')),
    discord_id  TEXT NOT NULL REFERENCES users(discord_id) ON DELETE CASCADE,
    UNIQUE (manga_id, role, discord_id)
);

-- Monthly invoice dispatch ledger. The primary key makes retries idempotent
-- for each member and billing period.
CREATE TABLE IF NOT EXISTS invoice_dispatches (
    discord_id       TEXT NOT NULL REFERENCES users(discord_id) ON DELETE CASCADE,
    billing_period   DATE NOT NULL,
    status           TEXT NOT NULL DEFAULT 'sending'
                     CHECK (status IN ('sending', 'sent', 'failed')),
    attempt_count    INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    last_attempt_at  TIMESTAMPTZ,
    sent_at          TIMESTAMPTZ,
    last_error       TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (discord_id, billing_period)
);
CREATE INDEX IF NOT EXISTS idx_invoice_dispatches_status
    ON invoice_dispatches(status, billing_period);

-- Explicit administrator retries are kept separately from the delivery row so
-- the actor and the prior state remain auditable after a later successful send.
CREATE TABLE IF NOT EXISTS invoice_dispatch_audit (
    id             SERIAL PRIMARY KEY,
    discord_id     TEXT NOT NULL,
    billing_period DATE NOT NULL,
    action         TEXT NOT NULL CHECK (action IN ('retry')),
    from_status    TEXT NOT NULL CHECK (from_status IN ('failed', 'sent')),
    to_status      TEXT NOT NULL CHECK (to_status = 'sending'),
    changed_by     TEXT NOT NULL,
    changed_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_invoice_dispatch_audit_dispatch
    ON invoice_dispatch_audit(discord_id, billing_period, changed_at DESC);

CREATE TABLE IF NOT EXISTS user_roles (
    id          SERIAL PRIMARY KEY,
    discord_id  TEXT NOT NULL REFERENCES users(discord_id) ON DELETE CASCADE,
    role        TEXT NOT NULL CHECK (role IN ('TL', 'CO-TL', 'PR', 'CO-PR', 'ED', 'CO-ED')),
    UNIQUE (discord_id, role)
);

CREATE TABLE IF NOT EXISTS chapters (
    id              SERIAL PRIMARY KEY,
    manga_id        INTEGER NOT NULL REFERENCES mangas(id) ON DELETE CASCADE,
    chapter_number  NUMERIC NOT NULL,
    drive_folder_id TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending_tl'
                     CHECK (status IN ('pending_tl','pending_pr','pending_ed','pending_review','published','rejected')),
    assigned_to     TEXT REFERENCES users(discord_id) ON DELETE SET NULL,
    assigned_at     TIMESTAMPTZ,
    images_count    INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (manga_id, chapter_number)
);

CREATE TABLE IF NOT EXISTS chapter_history (
    id           SERIAL PRIMARY KEY,
    chapter_id   INTEGER NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
    stage        TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    action       TEXT NOT NULL,
    amount_cents INTEGER NOT NULL DEFAULT 0,
    reason       TEXT,
    timestamp    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Ledger system
CREATE TABLE IF NOT EXISTS payments (
    id             SERIAL PRIMARY KEY,
    user_id        TEXT NOT NULL REFERENCES users(discord_id) ON DELETE CASCADE,
    amount_cents   INTEGER NOT NULL,
    paid_by        TEXT NOT NULL,
    payment_method TEXT NOT NULL,
    payment_info   TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Ledger system
CREATE TABLE IF NOT EXISTS transactions (
    id           SERIAL PRIMARY KEY,
    user_id      TEXT NOT NULL REFERENCES users(discord_id) ON DELETE CASCADE,
    chapter_id   INTEGER NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
    role         TEXT NOT NULL CHECK (role IN ('TL', 'CO-TL', 'PR', 'CO-PR', 'ED', 'CO-ED')),
    amount_cents INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'unpaid' CHECK (status IN ('unpaid', 'paid')),
    payment_id   INTEGER REFERENCES payments(id) ON DELETE SET NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, chapter_id, role)
);

-- Pricing config (editable via /set_price)
CREATE TABLE IF NOT EXISTS pricing_config (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL CHECK (value >= 0)
);
INSERT INTO pricing_config (key, value) VALUES ('tl_cents', 85) ON CONFLICT DO NOTHING;
INSERT INTO pricing_config (key, value) VALUES ('pr_cents', 85) ON CONFLICT DO NOTHING;
INSERT INTO pricing_config (key, value) VALUES ('ed_under_30', 150) ON CONFLICT DO NOTHING;
INSERT INTO pricing_config (key, value) VALUES ('ed_30_44', 200) ON CONFLICT DO NOTHING;
INSERT INTO pricing_config (key, value) VALUES ('ed_45_plus', 300) ON CONFLICT DO NOTHING;

-- Performance indexes
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX IF NOT EXISTS idx_manga_staff_manga_role ON manga_staff(manga_id, role);
CREATE INDEX IF NOT EXISTS idx_manga_staff_discord ON manga_staff(discord_id);
CREATE INDEX IF NOT EXISTS idx_chapters_manga_status ON chapters(manga_id, status);
CREATE INDEX IF NOT EXISTS idx_chapters_assigned ON chapters(assigned_to) WHERE assigned_to IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_chapters_status ON chapters(status);
CREATE INDEX IF NOT EXISTS idx_chapters_manga_chapter ON chapters(manga_id, chapter_number);
-- Covers /done query: filter by manga status + chapter status + assigned_to
CREATE INDEX IF NOT EXISTS idx_chapters_status_assigned ON chapters(status, assigned_to);
CREATE INDEX IF NOT EXISTS idx_transactions_user_status ON transactions(user_id, status);
CREATE INDEX IF NOT EXISTS idx_transactions_chapter ON transactions(chapter_id);
-- Monthly invoice: filter by created_at range per user — without this the FILTER scan is sequential
CREATE INDEX IF NOT EXISTS idx_transactions_created_at ON transactions(created_at);
CREATE INDEX IF NOT EXISTS idx_transactions_user_created ON transactions(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_chapter_history_chapter ON chapter_history(chapter_id);
CREATE INDEX IF NOT EXISTS idx_chapter_history_user_ts ON chapter_history(user_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id);
CREATE INDEX IF NOT EXISTS idx_user_roles_discord ON user_roles(discord_id);
CREATE INDEX IF NOT EXISTS idx_users_registered ON users(is_registered) WHERE is_registered = TRUE;
CREATE INDEX IF NOT EXISTS idx_users_discord_trgm ON users USING gin (discord_id gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_mangas_name_trgm ON mangas USING gin (manga_name gin_trgm_ops);
-- H2: prevent duplicate payment_info race condition (case-insensitive, only for registered users)
CREATE UNIQUE INDEX IF NOT EXISTS uq_users_payment_info_registered ON users (LOWER(payment_info)) WHERE payment_info IS NOT NULL AND is_registered = TRUE;
-- Migration: add assigned_at for claim timeout (safe for existing DBs)
ALTER TABLE chapters ADD COLUMN IF NOT EXISTS assigned_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_chapters_assigned_at ON chapters(assigned_at) WHERE assigned_to IS NOT NULL;
-- Remove redundant non-conditional index that is covered by unique partial index
DROP INDEX IF EXISTS idx_users_payment_info;

-- Audit tables
CREATE TABLE IF NOT EXISTS price_audit (
    id          SERIAL PRIMARY KEY,
    key         TEXT NOT NULL,
    old_value   INTEGER,
    new_value   INTEGER NOT NULL,
    changed_by  TEXT NOT NULL,
    changed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS user_audit (
    id          SERIAL PRIMARY KEY,
    discord_id  TEXT NOT NULL REFERENCES users(discord_id) ON DELETE CASCADE,
    field       TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT,
    changed_by  TEXT NOT NULL,
    changed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS manga_staff_audit (
    id          SERIAL PRIMARY KEY,
    manga_id    INTEGER NOT NULL REFERENCES mangas(id) ON DELETE CASCADE,
    discord_id  TEXT NOT NULL,
    role        TEXT NOT NULL,
    action      TEXT NOT NULL CHECK (action IN ('added','removed','auto_removed')),
    changed_by  TEXT NOT NULL,
    changed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_manga_staff_audit_manga ON manga_staff_audit(manga_id, changed_at DESC);
CREATE INDEX IF NOT EXISTS idx_manga_staff_audit_user ON manga_staff_audit(discord_id, changed_at DESC);

-- Drive chapter cache (reduces Drive API calls)
CREATE TABLE IF NOT EXISTS chapter_cache (
    manga_id        INTEGER NOT NULL REFERENCES mangas(id) ON DELETE CASCADE,
    chapter_number  NUMERIC NOT NULL,
    drive_folder_id TEXT NOT NULL,
    cached_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (manga_id, chapter_number)
);
CREATE INDEX IF NOT EXISTS idx_chapter_cache_manga ON chapter_cache(manga_id);

-- Background job queue for persistent Drive syncs (survives restarts)
CREATE TABLE IF NOT EXISTS background_jobs (
    id SERIAL PRIMARY KEY,
    job_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','done','failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_background_jobs_status ON background_jobs(status, created_at);
-- Migration: add discord/webhook columns to mangas (for single-category + webhook system)
ALTER TABLE mangas ADD COLUMN IF NOT EXISTS discord_channel_id TEXT;
ALTER TABLE mangas ADD COLUMN IF NOT EXISTS webhook_id TEXT;
ALTER TABLE mangas ADD COLUMN IF NOT EXISTS webhook_token TEXT;
ALTER TABLE mangas ADD COLUMN IF NOT EXISTS webhook_message_id TEXT;
-- Migration: cover_url for AdminDashboard (used in admin_ui.py)
ALTER TABLE mangas ADD COLUMN IF NOT EXISTS cover_url TEXT;
-- Migration: mangadex_url for MangaDex link button (next to Drive button)
ALTER TABLE mangas ADD COLUMN IF NOT EXISTS mangadex_url TEXT;
-- Migration: link transactions to payments
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS payment_id INTEGER REFERENCES payments(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_transactions_payment ON transactions(payment_id);

-- ═══════════════════════════════════════════════════════════════════
-- Migration: protect financial records from cascade deletion
-- Changing CASCADE → RESTRICT on payments & transactions so deleting
-- a user or chapter is blocked while financial records exist.
-- ═══════════════════════════════════════════════════════════════════

-- payments.user_id: CASCADE → RESTRICT
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.table_constraints
    WHERE constraint_name = 'payments_user_id_fkey'
      AND table_name = 'payments'
  ) THEN
    ALTER TABLE payments DROP CONSTRAINT payments_user_id_fkey;
    ALTER TABLE payments ADD CONSTRAINT payments_user_id_fkey
      FOREIGN KEY (user_id) REFERENCES users(discord_id) ON DELETE RESTRICT;
  END IF;
END $$;

-- transactions.user_id: CASCADE → RESTRICT
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.table_constraints
    WHERE constraint_name = 'transactions_user_id_fkey'
      AND table_name = 'transactions'
  ) THEN
    ALTER TABLE transactions DROP CONSTRAINT transactions_user_id_fkey;
    ALTER TABLE transactions ADD CONSTRAINT transactions_user_id_fkey
      FOREIGN KEY (user_id) REFERENCES users(discord_id) ON DELETE RESTRICT;
  END IF;
END $$;

-- transactions.chapter_id: CASCADE → RESTRICT
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.table_constraints
    WHERE constraint_name = 'transactions_chapter_id_fkey'
      AND table_name = 'transactions'
  ) THEN
    ALTER TABLE transactions DROP CONSTRAINT transactions_chapter_id_fkey;
    ALTER TABLE transactions ADD CONSTRAINT transactions_chapter_id_fkey
      FOREIGN KEY (chapter_id) REFERENCES chapters(id) ON DELETE RESTRICT;
  END IF;
END $$;

-- ═══════════════════════════════════════════════════════════════════
-- Migration: add missing indexes for frequently-queried columns
-- ═══════════════════════════════════════════════════════════════════
CREATE INDEX IF NOT EXISTS idx_user_audit_discord
    ON user_audit(discord_id, changed_at DESC);
CREATE INDEX IF NOT EXISTS idx_price_audit_key
    ON price_audit(key, changed_at DESC);
CREATE INDEX IF NOT EXISTS idx_mangas_status
    ON mangas(status);
CREATE INDEX IF NOT EXISTS idx_mangas_drive_folder
    ON mangas(drive_folder_id);

-- ═══════════════════════════════════════════════════════════════════
-- Migration: remove redundant indexes covered by UNIQUE constraints
-- ═══════════════════════════════════════════════════════════════════
DROP INDEX IF EXISTS idx_manga_staff_manga_role;   -- covered by UNIQUE(manga_id, role, discord_id)
DROP INDEX IF EXISTS idx_user_roles_discord;        -- covered by UNIQUE(discord_id, role)
DROP INDEX IF EXISTS idx_chapters_manga_chapter;    -- covered by UNIQUE(manga_id, chapter_number)
DROP INDEX IF EXISTS idx_chapter_cache_manga;       -- covered by PK(manga_id, chapter_number)
DROP INDEX IF EXISTS idx_chapters_status;           -- covered by idx_chapters_status_assigned

-- ═══════════════════════════════════════════════════════════════════
-- Migration: data integrity constraints (NOT VALID = new rows only)
-- After verifying existing data, run VALIDATE CONSTRAINT manually.
-- ═══════════════════════════════════════════════════════════════════
DO $$ BEGIN
  ALTER TABLE payments ADD CONSTRAINT chk_payments_amount
      CHECK (amount_cents > 0) NOT VALID;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  ALTER TABLE transactions ADD CONSTRAINT chk_transactions_amount
      CHECK (amount_cents >= 0) NOT VALID;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  ALTER TABLE chapters ADD CONSTRAINT chk_chapters_images
      CHECK (images_count IS NULL OR images_count >= 0) NOT VALID;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  ALTER TABLE chapters ADD CONSTRAINT chk_chapters_assignment_consistency
      CHECK (
          (assigned_to IS NULL AND assigned_at IS NULL)
          OR (assigned_to IS NOT NULL AND assigned_at IS NOT NULL)
      ) NOT VALID;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- Prevent multiple mangas from linking to the same Google Drive folder
CREATE UNIQUE INDEX IF NOT EXISTS uq_mangas_drive_folder
    ON mangas(drive_folder_id);

