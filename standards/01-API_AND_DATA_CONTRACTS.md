# 01 — API & Data Contracts Standards — yuna-next

> **MANDATORY CONTRACT**: Every SSDLC ticket touching network communication, backend endpoints, or data payloads MUST strictly adhere to these standards. Non-compliant APIs or ad-hoc requests will be rejected during Code Review.

---

## 1. Zero Raw `fetch()` Rule
- **Client**: Every client-side API call in `client/yuna-app/` MUST use the central typed client:
  ```typescript
  import { apiClient } from "@/api/client";
  // All routes are strictly validated against OpenAPI schema:
  // contracts/openapi/yuna-next-v1.json
  ```
- **Prohibited**: Ad-hoc `fetch("https://...")` or custom `axios` instances inside UI components.

---

## 2. URL Naming & Versioning Conventions
- **Base Prefix**: All public API endpoints must be versioned: `/v1/...`
- **Resource Naming**: Plural nouns in lower-kebab-case (e.g., `/v1/shopping-sessions`, `/v1/user-profiles`).
- **RPC / Action Verbs**: Use sub-resources for domain actions:
  - `POST /v1/shopping-sessions/{id}/checkout`
  - `POST /v1/account/deletion-request`

---

## 3. Standard Pagination Contract (Cursor-Based)
Never use arbitrary `offset/page` patterns. All list/feed endpoints MUST conform to:

### Request Query Parameters:
```typescript
{
  cursor?: string;   // Opaque pagination token returned from previous page
  limit?: number;    // Default: 20, Max: 100
}
```

### Response Envelope:
```json
{
  "items": [ /* array of resource entities */ ],
  "next_cursor": "eyJpZCI6MTIzfQ==",
  "has_more": true
}
```

---

## 4. Standard Error Response Envelope
Every non-2xx response from backend services MUST return the uniform error schema:

```json
{
  "error": {
    "code": "RESOURCE_NOT_FOUND",
    "message": "The requested shopping session does not exist or has expired.",
    "retryable": false,
    "details": {
      "field": "session_id",
      "reason": "invalid_uuid_v4"
    }
  }
}
```

- **Error Codes Format**: `SCREAMING_SNAKE_CASE` (e.g. `INVALID_TOKEN`, `RATE_LIMITED`, `INTERNAL_DEPENDENCY_ERROR`).
- **`retryable` Flag**: Boolean telling the client whether an automatic exponential backoff retry is safe.

---

## 5. Idempotency & Auth Headers
- **Mutation Operations (`POST`, `PUT`, `DELETE`)**: When executing critical transactions (orders, payments, profile mutations), the client must supply:
  ```http
  X-Idempotency-Key: <UUID-v4>
  ```
- **Authentication**: `Authorization: Bearer <TOKEN>` supplied via the central interceptor in `src/api/client.ts`.
