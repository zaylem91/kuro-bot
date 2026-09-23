# ⚔️ Kuro Bot

<p align="center">
  <img src="dashboard/favicon.ico" width="130" height="130" alt="Kuro Bot Logo">
  <br>
  <b>Kuro Bot</b> — Enterprise Resource Planning (ERP) & Automation Engine for Scanlation Teams
</p>

[![Python 3.14](https://img.shields.io/badge/python-3.14%20%7C%203.11+-3776AB.svg?style=flat&logo=python&logoColor=white)](https://www.python.org/)
[![PostgreSQL 18](https://img.shields.io/badge/PostgreSQL-18%20%7C%2016+-336791.svg?style=flat&logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Hikari 2.6](https://img.shields.io/badge/Discord-Hikari%202.6-5865F2.svg?style=flat&logo=discord&logoColor=white)](https://github.com/hikari-py/hikari)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED.svg?style=flat&logo=docker&logoColor=white)](https://www.docker.com/)
[![CI](https://github.com/LyndoDev/kuro-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/LyndoDev/kuro-bot/actions)
[![Tests](https://img.shields.io/badge/tests-69%2F69%20passing-brightgreen.svg?style=flat&logo=pytest&logoColor=white)](tests/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg?style=flat)](#license)

**Kuro Bot** is a high-performance, enterprise-grade Enterprise Resource Planning (ERP) and automation system engineered specifically for scanlation teams. Built on **Hikari**, **PostgreSQL 18**, **Google Drive API**, and a modern **aiohttp Web Dashboard**, it automates the entire manga publication pipeline from raw acquisition to final publication, staff workload balancing, and atomic financial accounting.

---

## 📑 Table of Contents

- [Key Features](#-key-features)
- [System Architecture](#-system-architecture)
- [Tech Stack](#-tech-stack)
- [Prerequisites](#-prerequisites)
- [Quick Start](#-quick-start)
  - [Local Installation](#1-local-installation)
  - [Docker Deployment](#2-docker-deployment)
- [Configuration (.env)](#-configuration-env)
- [Google Drive Integration](#-google-drive-integration)
- [Remote Web Dashboard](#-remote-web-dashboard)
- [Discord Slash Commands & UI](#-discord-slash-commands--ui)
- [Project Structure](#-project-structure)
- [Testing & Quality Assurance](#-testing--quality-assurance)
- [Security & Data Integrity](#-security--data-integrity)
- [License](#-license)

---

## 🌟 Key Features

### 🌐 1. Modern Remote Web Dashboard (`/dashboard`)
- **Dark Glassmorphic UI:** Fully responsive interface featuring dynamic RTL/LTR support and multi-language internationalization:
  - 🇪🇬 Arabic (`ar`)
  - 🇬🇧 English (`en`)
  - 🇪🇸 Spanish (`es`)
  - 🇫🇷 French (`fr`)
  - 🇷🇺 Russian (`ru`)
- **Hardened Authentication:** Token & server-side session authentication with `HttpOnly` and `Secure` cookies, constant-time comparisons (`hmac.compare_digest`), IP brute-force lockdown (5 failed attempts = 10m ban), and trusted proxy validation.
- **Read-Only Guest Share Links:** Generate time-limited (15m, 1h, 24h) shareable links for auditors or guests with mutation protection.
- **Manga & Chapter Hub:** Add and edit series, create and link Discord channels, assign staff with live Discord avatars and instant search, inspect chapters, trigger Drive syncs, and approve/reject with feedback.
- **Finance & Payout Center:** View unpaid earnings, process atomic batch payouts, configure per-page pricing rules, and export ledgers directly to **CSV** (`/api/finance/export`).
- **Database & Backups Hub:** Inspect schema versions and connection pool stats, generate on-demand `.sql` dumps, and browse/download historical backups with path-traversal protection and admin-only authorization.
- **Background Jobs Monitor:** Live real-time inspection of background task statuses, payloads, execution attempts, and error logs.

### 🔄 2. Multi-Stage Chapter Pipeline
- **Strict Sequential Lifecycle:**
  $$\text{Pending TL} \longrightarrow \text{Pending PR} \longrightarrow \text{Pending ED} \longrightarrow \text{Pending Review} \longrightarrow \text{Published}$$
- **State Regression Protection:** Google Drive synchronization will never downgrade chapters that are finished, under review, or published.
- **Double-Payout Prevention:** Chapters resubmitted after quality control corrections or reopens are shielded against duplicate ledger transactions.
- **48-Hour Claim Timeout:** Automated background daemon releases stale chapter claims after 48 hours, notifies the member via DM, and updates channel status embeds automatically.
- **Discord Components V2:** Polished interactive modals, dropdowns, and button containers across all user flows.

### 📁 3. Google Drive Autonomous Deep Sync
- **Modular Mixin Architecture:** Decoupled into specialized services for authentication/token health, folder hierarchy management, and concurrency-controlled file operations.
- **Full URL & ID Parsing:** Accepts full Google Drive links (`drive.google.com/drive/folders/...`, `open?id=...`) or folder IDs, auto-extracting identifiers.
- **Automated Permission Management:** Automatically grants or revokes Google Drive edit permissions (`writer`) to staff members when they join, update their email, or leave a manga.
- **Smart Validation & Caching:** Validates image files, caches API discovery documents, uses an asynchronous concurrency semaphore (limit 8), and performs 60-second incremental cache sweeps.
- **Clock Skew Detection:** Automatically validates host clock synchronization against Google servers on startup to prevent JWT `invalid_grant` authentication failures.

### 💳 4. Financial Accounting & Payouts
- **Atomic Transactions:** Payouts link historical transaction records directly to payment receipts (`payment_id` foreign key) in atomic database transactions.
- **Data Integrity & Cascade Shields:** Foreign keys on `payments` and `transactions` utilize `ON DELETE RESTRICT` to prevent accidental loss of financial audit history.
- **Configurable Page Pricing:** Dynamic rates for Translators (TL), Proofreaders (PR), and tiered rates for Editors (<30, 30-44, 45+ pages) with non-negative database constraints (`CHECK (value >= 0)`).
- **Automated Monthly Billing:** Scheduled cron job generates itemized invoices and dispatches them to finance channels with database-backed idempotency and audit logs.

### ⚙️ 5. Persistent Task Queue & Observability
- **Database-Backed Job Queue:** Resilient asynchronous queue (`background_jobs` table) surviving process restarts.
- **Exponential Backoff:** Up to 3 automatic retries (`MAX_ATTEMPTS = 3`) with backoff delays (2s, 4s, 8s).
- **Dead-Letter Webhook Alerts:** Failed jobs send rich Embed alert notifications to audit channels with error traces.
- **Telemetry & Health Endpoints:** Built-in `GET :8080/health` (uptime, DB pool stats, cache sizes) and `GET :8080/metrics` (Prometheus metrics), with optional PII-stripped Sentry integration.

### 👥 6. Workload Intelligence & Staff Lifecycle
- **Workload Analyzer (`/workload`):** Real-time analytics categorizing members by active workload (🔴 High 5+, 🟡 Medium 3-4, 🟢 Low 1-2, ⚪ Idle).
- **Smart Staff Recommender:** Recommends the least-loaded qualified members when assigning series roles.
- **Privacy-First Registration:** Uses private Discord modals (`/register`) to collect Gmail, timezone, and payment info securely without exposing PII in public chat channels.
- **Member Rejoin & Leave Hooks:** Welcomes rejoining staff with a summary of their previous roles and pending balances; gracefully cleans up departed members and revokes Drive folder permissions.

### 🗄️ 7. Version-Controlled Database Migrations
- **Automated Migration Engine:** Sequential, transactional migration runner (`bot/database/migrate.py`) tracking versions in `schema_versions`.
- **Zero-Downtime Safe DDL:** Applies column additions, partial indexes, and check constraints safely without data loss.

---

## 🏗 System Architecture

```mermaid
flowchart TD
    subgraph Clients["Clients & Interfaces"]
        DiscordUsers["Discord Users & Staff<br/>(Slash Commands & Components V2)"]
        WebBrowser["Web Browser (Admin & Guests)<br/>(Remote Glassmorphic Dashboard)"]
    end

    subgraph Core["Bot Application Core (Hikari / Python 3.14)"]
        BotEngine["Lightbulb & Miru Engine<br/>(Command Router & UI Views)"]
        WebServer["Aiohttp Web Server (:8080)<br/>(REST API, Dashboard & Database Hub)"]
        TaskQueue["Persistent Task Queue Worker<br/>(Background Sync Jobs)"]
        DriveSync["Drive Deep Sync Engine<br/>(Folders, Files & Permissions)"]
        Daemons["Background Daemons<br/>(48h Timeout, Invoices & Daily Backups)"]
        Migrator["Database Migration Runner<br/>(Transactional Schema Versioning)"]
    end

    subgraph Storage["External Services, Storage & Databases"]
        Postgres[("PostgreSQL 18 Database<br/>(Relational Data & Persistent Queue)")]
        BackupStorage[("Disk Storage (backups/)<br/>(Rotated .sql Database Dumps)")]
        GoogleDrive[("Google Drive API v3<br/>(Service Account & Shared Drives)")]
        DiscordAPI["Discord REST & Gateway API<br/>(Events, Embeds & Notifications)"]
        Sentry["Sentry Monitoring<br/>(PII-Stripped Error Telemetry)"]
    end

    DiscordUsers -->|Gateway Interactions| BotEngine
    WebBrowser -->|REST API, Sessions & SQL Downloads| WebServer

    BotEngine -->|Send Messages & Modals| DiscordAPI
    BotEngine -->|Read/Write Records| Postgres

    WebServer -->|Session Validation & Status| Postgres
    WebServer -->|On-Demand Dumps & Downloads| BackupStorage
    WebServer -->|Health & Crash Reporting| Sentry

    TaskQueue -->|Poll & Complete Jobs| Postgres
    TaskQueue -->|Execute Sync Jobs| DriveSync

    DriveSync -->|Scan Folders & Permissions| GoogleDrive
    DriveSync -->|Upsert Chapters & Ledgers| Postgres

    Daemons -->|Unclaim Expired Chapters| Postgres
    Daemons -->|Send DM Reminders & Invoices| DiscordAPI
    Daemons -->|Nightly Automated pg_dump| BackupStorage

    Migrator -->|Execute Migration DDL| Postgres
```

---

## 💻 Tech Stack

| Component | Technology | Description |
| :--- | :--- | :--- |
| **Language** | Python 3.14 / 3.11+ | High-performance asynchronous runtime (CPython 3.14.7, uvloop on Linux) |
| **Discord Engine** | `hikari` 2.6.0 | Fast, type-safe Discord Gateway & REST client |
| **Command Framework** | `hikari-lightbulb` 3.2.6 | Slash command routing, option autocompletion, and cooldowns |
| **UI Components** | `hikari-miru` 4.3.1 | Discord Components V2, interactive buttons, menus, and modals |
| **Database** | PostgreSQL 18+ / 16+ + `asyncpg` 0.31.0 | Non-blocking connection pool (5–20), `pg_trgm` fuzzy matching, migrations |
| **Google Drive** | `aiogoogle` 5.19.0 | Async Drive v3 API with semaphore limit (8) & discovery caching |
| **Web Server** | `aiohttp` 3.14.3 | High-concurrency dashboard API & health service |
| **Observability** | Prometheus / Sentry 2.68.1 | Live metrics, health endpoints, and error tracking |
| **Containerization** | Docker & Docker Compose | Multi-stage production container with unprivileged user |

---

## 📋 Prerequisites

Before deploying Kuro Bot, ensure you have:
1. **Python 3.11 – 3.14+** installed (verified on Python 3.14.7).
2. **PostgreSQL 16 – 18+** server running (verified on PostgreSQL 18.6).
3. **Discord Bot Token** with `GuildMembers` and `MessageContent` privileged gateway intents enabled.
4. **Google Cloud Service Account** with Google Drive API enabled and editor access to your root scanlation folder.

---

## 🚀 Quick Start

### 1. Local Installation

```bash
# 1. Clone the repository
git clone https://github.com/LyndoDev/kuro-bot.git
cd kuro-bot

# 2. Create and activate a virtual environment
python -m venv venv

# On Windows (PowerShell):
venv\Scripts\activate
# On Linux / macOS:
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure your environment variables
cp .env.example .env
# Edit .env with your credentials

# 5. Run database migrations & launch the bot
python -O run.py
```

### 2. Docker Deployment

```bash
# Build and launch all services (Bot + PostgreSQL 18) in detached mode
docker compose up --build -d

# View live application logs
docker compose logs -f bot
```

Database migrations (`bot/database/migrations/`) run automatically on startup via `init_db`.

---

## ⚙️ Configuration (.env)

| Variable | Required | Default | Description |
| :--- | :---: | :---: | :--- |
| `DISCORD_TOKEN` | **Yes** | — | Discord Bot application token |
| `DATABASE_URL` | **Yes** | — | PostgreSQL connection URI (`postgresql://user:pass@host:5432/db`) |
| `GUILD_ID` | **Yes** | — | Discord Server (Guild) Snowflake ID for instant slash command sync |
| `ADMIN_ROLE_ID` | **Yes** | — | Role ID for Bot Administrators |
| `TEAM_LEADER_ROLE_ID` | **Yes** | — | Role ID for Scanlation Team Leaders |
| `TL_ROLE_ID` | Optional | — | Role ID for Translators (auto-assigned upon `/register`) |
| `PR_ROLE_ID` | Optional | — | Role ID for Proofreaders (auto-assigned upon `/register`) |
| `ED_ROLE_ID` | Optional | — | Role ID for Editors (auto-assigned upon `/register`) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | **Yes** | `bot/Service Account.json` | Path or raw JSON content of Google Cloud Service Account key |
| `DRIVE_ROOT_FOLDER_ID` | **Yes** | — | Root Google Drive folder ID containing series directories |
| `DASHBOARD_TOKEN` | Optional | — | Secure token for Web Dashboard access (leave empty to disable) |
| `HEALTH_PORT` | Optional | `8080` | Port for Dashboard, Health, and Metrics HTTP server |
| `HEALTH_HOST` | Optional | `127.0.0.1` | Host interface for health server (`0.0.0.0` inside Docker) |
| `HEALTH_TOKEN` | Optional | — | Optional Bearer token for `/health` and `/metrics` |
| `TEAM_NAME` | Optional | `Kuro` | Name of your scanlation team used across embeds and UI |
| `MANGA_CATEGORY_NAME` | Optional | `📚 Mangas` | Category name for ongoing manga channels |
| `MANGA_CATEGORY_PAUSED_NAME` | Optional | `⏸️ Paused Mangas` | Category name for paused manga channels |
| `MANGA_CATEGORY_DROPPED_NAME`| Optional | `❌ Dropped Mangas` | Category name for dropped manga channels |
| `SENTRY_DSN` | Optional | — | Sentry DSN for error telemetry (PII-stripped) |
| `MONTHLY_TIMEZONE` | Optional | `UTC` | Timezone for monthly financial resets (e.g. `UTC`, `America/New_York`) |
| `DASHBOARD_ALLOWED_IPS` | Optional | — | Comma-separated IP allowlist for dashboard access |
| `DASHBOARD_CORS_ORIGINS` | Optional | `*` | Allowed CORS origins for dashboard API |

---

## 📁 Google Drive Integration

1. Create a project in [Google Cloud Console](https://console.cloud.google.com/) and enable the **Google Drive API**.
2. Create a Service Account, generate a JSON Key, and save it as `bot/Service Account.json` (or set `GOOGLE_SERVICE_ACCOUNT_JSON`).
3. Open your Root Google Drive folder, click **Share**, and grant **Editor** access to the service account email (`client_email`).
4. Organize your series subfolders using standard scanlation directories:
   ```text
   Root Scanlation Folder/
   └── Solo Leveling/
       ├── RAW/
       ├── TL/
       ├── PR/
       └── ED/ (or Final/)
   ```
5. When adding series via `/admin` or the Web Dashboard, paste full Drive URLs directly:
   `https://drive.google.com/drive/folders/1A2b3C4d5E...`

---

## 🖥️ Remote Web Dashboard

The web dashboard is served directly from the embedded `aiohttp` HTTP service on `HEALTH_PORT` (default `8080`).

### 1. Generate an Access Token
```bash
python scripts/generate_dashboard_token.py
```
Copy the generated 64-character token and set it in your `.env` as `DASHBOARD_TOKEN`.

### 2. Access the Dashboard
Navigate to:
```text
http://localhost:8080/dashboard/
```
Authenticate with your `DASHBOARD_TOKEN`.

### 3. Dashboard Modules
- **Overview:** Summary counters, uptime, financial balances, breakdown charts, and live team activity timeline.
- **Manga Hub:** Real-time series status toggles, Discord channel creation, Drive deep sync, and staff management with live avatar selection.
- **Chapters Hub:** Status filtering, chapter re-opening, quality control approvals/rejections with comments, and assignments.
- **Staff Hub:** Workload inspection, permission badges, Drive email and payment method management, and one-click payout execution.
- **Finance Hub:** Unpaid transaction breakdown, one-click payout execution, per-page rate adjustments, and instant CSV exports (`/api/finance/export`).
- **Background Jobs:** Monitor queued and executing tasks, view retries, and check error payloads.
- **Internationalization (i18n):** Switch instantly between Arabic, English, Spanish, French, and Russian with RTL/LTR layout adaptation.

---

## 💬 Discord Slash Commands & UI

| Command | Category | Description | Permissions |
| :--- | :--- | :--- | :--- |
| `/register` | Staff | Register role, timezone, Drive Gmail, and payout wallet via secure modal | Everyone |
| `/staff` | Staff | Open the interactive Staff Workspace (Claim, Submit, Balance, Edit Profile) | Staff |
| `/claim` | Staff | Interactive selection to claim chapters for 48 hours | Staff |
| `/done` | Staff | Submit finished work with automated image validation | Staff |
| `/unclaim` | Staff | Release a claimed chapter back to the pool | Staff / Admin |
| `/assign` | Management | Directly assign a chapter to a registered member | Admin |
| `/workload` | Management | Inspect staff workload breakdown sorted by active load | Admin |
| `/admin` | Management | Open Admin Dashboard (Projects, Staff Hub, Workload, Finance) | Admin |
| `/qc` | Quality Control | Review pending chapters: approve, reject, reopen, or sync | Admin |
| `/audit` | Audit | Inspect audit logs for member changes & staff assignments | Admin |
| `/setup_finance` | Finance | Initialize or configure guild payout channels | Team Leader |
| `/set_price` | Finance | Set page rate in cents for specific roles | Team Leader |
| `/price_history`| Finance | View rate adjustment history and audit logs | Team Leader |

---

## 📂 Project Structure

```text
kuro-bot/
├── .env.example                   # Template environment configuration
├── Dockerfile                     # Multi-stage production container (Python 3.14-slim)
├── docker-compose.yml             # Orchestration for bot and PostgreSQL 18
├── pytest.ini                     # Isolated pytest test runner & cache configuration
├── requirements.txt               # Pinned Python dependencies
├── run.py                         # Application entrypoint
├── reset_db.py                    # Database schema reset & migration runner
├── bot/
│   ├── __main__.py                # Bot startup, extension loading, lifecycle hooks
│   ├── config.py                  # Validated environment loader, emojis, & constants
│   ├── core.py                    # Shared service singletons (Bot, Miru, Drive, Health)
│   ├── models.py                  # Domain data models, contracts, and TypedDict schemas
│   ├── api/                       # Modular REST API for Remote Web Dashboard
│   │   ├── core.py                # Session auth, token guards, rate limiting, helpers
│   │   ├── router.py              # Central aiohttp route definitions
│   │   ├── manga.py               # Manga management, status updates, staff assignments
│   │   ├── chapters.py            # Chapter listing & QC actions (approve/reject/reopen)
│   │   ├── staff.py               # Staff listing, profile updates, and payout execution
│   │   ├── finance.py             # Finance overview and CSV export endpoints
│   │   ├── database.py            # Database status, backup creation, inspection & restore
│   │   └── system.py              # Dashboard static files, locales, stats, jobs, pricing
│   ├── database/
│   │   ├── connection.py          # Asynchronous PostgreSQL connection pool (asyncpg)
│   │   ├── migrate.py             # Transactional database migration runner
│   │   └── migrations/
│   │       └── 001_baseline.sql   # Relational DDL, constraints, RESTRICT rules, pg_trgm
│   ├── extensions/                # Discord slash command extensions (Hikari Lightbulb)
│   │   ├── admin.py               # /admin & /workload management commands
│   │   ├── audit.py               # /audit log inspection command
│   │   ├── claim.py               # /claim, /assign, /unclaim handlers
│   │   ├── events.py              # Guild join/leave & member lifecycle events
│   │   ├── finance.py             # /setup_finance, /set_price, /price_history commands
│   │   ├── profile.py             # /register (private modal) & /staff commands
│   │   └── workflow_commands/     # Modular chapter submission & QC pipeline
│   │       ├── done.py            # /done submission command
│   │       ├── qc.py              # /qc quality control command
│   │       ├── workflow_db.py     # State transitions & double-payout protection
│   │       ├── workflow_state.py  # Stage definitions & role mapping
│   │       └── workflow_ui.py     # Components V2 views, modals, & warnings
│   ├── services/                  # Business logic & core engines
│   │   ├── autocomplete.py        # Dynamic slash command autocompleters
│   │   ├── cache.py               # In-memory TTL caches for workload & options
│   │   ├── cooldown.py            # Command cooldown enforcement
│   │   ├── drive/                 # Modular Google Drive service engine
│   │   │   ├── auth.py            # Service account auth & clock skew verification
│   │   │   ├── folders.py         # Folder sanitization, hierarchy & permission management
│   │   │   └── files.py           # Concurrency-controlled deep sync (semaphore limit 8)
│   │   ├── health.py              # /health & Prometheus /metrics HTTP server
│   │   ├── manga_access.py        # Dynamic Discord channel permission grant/revoke
│   │   ├── member_picker.py       # Reusable member selection helper
│   │   ├── notifications.py       # Notification and alert helpers
│   │   ├── permissions.py         # Role-based access control (RBAC) checks
│   │   ├── pricing.py             # Transaction pricing calculation engine
│   │   ├── role_sync.py           # Discord role synchronization
│   │   ├── staff_ui.py            # Staff Workspace & dashboard components
│   │   ├── task_queue.py          # PostgreSQL-backed persistent background queue
│   │   ├── ui.py                  # Embed and component helpers
│   │   └── webhook_sender.py      # Embed builders & webhook delivery dispatcher
│   ├── tasks/                     # Scheduled background daemons
│   │   ├── assignment_timeout.py  # 48-hour claim expiration watchdog
│   │   ├── backup_loop.py         # Automated SQL backup daemon
│   │   ├── monthly_invoice.py     # Monthly billing generator & dispatcher
│   │   └── monthly_reset_loop.py  # Periodic cycle maintenance daemon
│   └── ui/                        # Modular Discord Components V2 UI Views
│       └── admin/
│           ├── core.py            # UI helpers, navigation, and pagination views
│           ├── dashboard.py       # Main Admin Control Panel view
│           ├── finance.py         # Admin finance settings & payout views
│           ├── manga_modals.py    # Modals for manga creation and settings
│           ├── manga_views.py     # Manga inspection & staff assignment views
│           ├── qc.py              # Admin Quality Control review views
│           ├── staff.py           # Staff workspace management views
│           └── workload.py        # Workload intelligence & recommender views
├── dashboard/                     # Web Dashboard Front-End
│   ├── index.html                 # Single-page dashboard application markup
│   ├── app.js                     # Client-side application logic & API client
│   ├── style.css                  # Dark glassmorphic responsive stylesheet
│   └── locales/                   # Decoupled internationalization dictionaries
│       ├── ar.js                  # Arabic translation
│       ├── en.js                  # English translation
│       ├── es.js                  # Spanish translation
│       ├── fr.js                  # French translation
│       └── ru.js                  # Russian translation
├── scripts/
│   ├── audit_historical_work.py   # Historical Drive work audit & report generator
│   ├── backup.py                  # On-demand PostgreSQL database backup utility
│   ├── generate_dashboard_token.py# Cryptographically secure dashboard token generator
│   └── post-merge.sh              # Git post-merge hook script
└── tests/                         # Automated test suite (pytest-asyncio)
    ├── test_audit_historical.py   # Verification for historical audit engine
    ├── test_dashboard_api.py      # End-to-end tests for dashboard REST API & auth
    ├── test_database_backup_api.py# End-to-end tests for database inspector & backup API
    ├── test_drive_extract.py      # Tests for Google Drive URL/ID extraction
    ├── test_extract.py            # Mention & ID extraction logic tests
    ├── test_monthly_invoice.py    # Tests for monthly invoice generation & dispatch
    ├── test_permissions.py        # Tests for RBAC permission checks
    ├── test_pricing.py            # Tests for pricing calculation engine
    ├── test_stress_and_concurrency.py # Breaking-point stress tests (100 claims, flood, semaphores)
    └── test_task_queue.py         # Tests for background queue retries & alerts
```

---

## 🧪 Testing & Quality Assurance

The codebase includes an automated test suite verifying workflow state machines, Google Drive parsers, background queues, and API endpoints.

```bash
# Run the complete test suite
pytest -v

# Run with concise summary
pytest -q
```

All 69 unit, integration, and stress tests pass with 100% test reliability and 0 warnings:
```text
============================== 69 passed in ~4.5s ===============================
```

### 💥 Stress & Breaking Point Suite (`test_stress_and_concurrency.py`)
- **100 Concurrent Chapter Claims:** Verifies that exactly one worker succeeds and 99 collisions fail gracefully without database deadlocks.
- **REST API Flood & Brute-Force Lockdown:** Floods 200+ requests, validates instant 429 response upon IP ban, and ensures rate limiter memory remains stable without memory leaks.
- **Concurrent Payout Race Conditions:** Confirms that parallel payout execution for the same member is prevented with pessimistic row locking.
- **Task Queue Saturation:** Tests worker backpressure and atomic job consumption under 50 simultaneous background tasks.
- **Google Drive Semaphore Ceiling:** Rigorously verifies that the asynchronous semaphore rigidly caps concurrent Drive API calls at 8.

### 🤖 Continuous Integration (GitHub Actions)
Every pull request and push to `main` triggers a complete end-to-end CI pipeline:
- **Environment:** Ephemeral runner with official **Python 3.14** and **PostgreSQL 18** (`postgres:18-alpine`).
- **Compilation Check:** Validates and bytecode-compiles all modules recursively via `compileall`.
- **Schema & Migration Verification:** Executes relational schema creation, constraints, foreign keys, and migrations on a fresh database.
- **Full Test Suite:** Runs the complete 69-test suite in strict asynchronous mode (`pytest-asyncio`).

---

## 🔒 Security & Data Integrity

- **SQL Injection Immunity:** All database interactions strictly utilize parameterized queries (`asyncpg` syntax `$1, $2, ...`). Dynamic string interpolation in SQL queries is disallowed.
- **Timing-Attack Resistant Auth:** Dashboard token verification uses `hmac.compare_digest` to prevent side-channel timing attacks.
- **Session Security & Proxy Verification:** Dashboard sessions use cryptographically generated 256-bit tokens, stored in `HttpOnly`/`Secure` cookies, with remote IP validation protecting against spoofed headers.
- **Concurrency & Race Condition Defenses:** Chapter claims, status updates, and payouts utilize pessimistic row locks (`SELECT ... FOR UPDATE`) inside atomic database transactions.
- **Financial Record Cascade Protection:** Foreign keys from `payments` and `transactions` enforce `ON DELETE RESTRICT`, preventing accidental data deletion if a user or chapter is removed.
- **Double-Payout Safeguards:** Chapter submission pipeline checks historical payouts to ensure resubmitted or reopened chapters never trigger duplicate payment ledger entries.
- **PII Protection:** User registration utilizes private Discord modals (`/register`) to prevent personal emails and payment details from being posted in guild channels.
- **Strict Input Sanitization:** Discord mentions and channel descriptions are filtered against `@everyone` and `@here` mass-ping exploits.
- **Role-Based Access Control (RBAC):** Administrative actions require verification against both Discord roles and database flags before initiating mutations.

---

## 📄 License

Distributed under the MIT License. See `LICENSE` for more information.
