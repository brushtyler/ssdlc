# 04 — Storage, State & Backend Migration Standards — yuna-next

> **MANDATORY CONTRACT**: Standards for client-side storage, backend state persistence, and database schema migrations.

---

## 1. Client Storage Partitioning (Secrets vs Volatile Cache)

| Storage Tier | Technology | Permitted Data | Prohibited Data |
| :--- | :--- | :--- | :--- |
| **Secure Vault** | `tokenVault.ts` (`expo-secure-store`) | Auth JWTs, refresh tokens, session credentials | Large blobs, analytics, UI preferences |
| **Encrypted Operations** | `accountDeletionReceiptStore.ts` | Proof receipts, legal audit anchors | Unencrypted PII |
| **Volatile Client Cache** | React state, React Query, memory store | Feed items, product cards, preview images | Secrets, credentials |

- **Security Invariant**: Never store auth tokens or private user identifiers in plaintext `AsyncStorage` or browser `localStorage`.

---

## 2. Backend Database Schema Evolution (Alembic)
Every migration in `yuna_next/migrations/` must be **zero-downtime and backward-compatible**:

### Two-Phase Schema Migration Pattern:
1. **Phase 1 (Expand)**:
   - Add new columns as `nullable=True` (or with server default).
   - Old and new versions of the application can run simultaneously against the database.
2. **Phase 2 (Migrate Data & Enforce)**:
   - Backfill data asynchronously.
   - Enforce constraints (`NOT NULL`, indexes) using non-blocking index creation (`CREATE INDEX CONCURRENTLY` in Postgres).

### Prohibited in Single-Step Migrations:
- Synchronously dropping a column currently in use (`DROP COLUMN`).
- Synchronously renaming a column (`RENAME COLUMN`).
- Adding a `NOT NULL` constraint without a server default on a populated table.

---

## 3. Offline-First & Outbox Queue
- Critical operations initiated on the client (e.g. cart updates, messages, deletion requests) must be recorded into the persistent outbox store before network dispatch.
- If network is unreachable, operations remain queued and automatically reconcile upon reconnection.
