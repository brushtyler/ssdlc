# 05 — Error Handling, Observability & Logging Standards — yuna-next

> **MANDATORY CONTRACT**: Every error surface, logging call, and user-facing feedback state MUST conform to these requirements.

---

## 1. Structured Logging Standard
- **Forbidden**: Raw `console.log()`, `console.warn()`, or `print()` statements in production code.
- **Client**: Always use the central module logger:
  ```typescript
  import { createLogger } from "@/utils/logger";
  const log = createLogger("ModuleName");

  log.info("Operation started", { sessionId, action: "checkout" });
  log.error("Operation failed", { error: err.message });
  ```
- **Backend**: Always use `yuna_next.observability` structured logger with request context.

---

## 2. Strict PII & Secret Sanitization
Never log or expose:
- Plaintext passwords or auth tokens (`Authorization` header bodies).
- Full email addresses (mask as `j***@domain.com` if required for diagnostics).
- Credit card numbers, CVVs, or payment token secrets.
- Private message contents or raw biometric payloads.

---

## 3. Mandatory 4-State UI Contract & Recovery
Every async screen or interactive component must handle all 4 states:

1. **Loading State**:
   - Must show a skeleton or `<Loading>` spinner with a **hard timeout** (max 5 seconds).
   - If the timeout expires without network response, it must transition to the **Error State** (never stay stuck in loading).
2. **Empty State**:
   - Clear icon/graphic, explanatory copy, and a primary CTA (e.g. "Explore Products").
3. **Error State**:
   - User-friendly message explaining the issue in plain language.
   - Actionable recovery trigger: `<Button label="Riprova" onPress={onRetry} />`.
4. **Success State**:
   - The primary rendered data view.
