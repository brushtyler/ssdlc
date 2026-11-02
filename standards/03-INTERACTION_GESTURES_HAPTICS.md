# 03 — Interaction Design, Gestures & Haptics Standards — yuna-app

> **MANDATORY CONTRACT**: Every touch interaction, gesture, animation, and tactile feedback in `client/yuna-app` MUST strictly adhere to this specification.

---

## 1. Unified Haptic Feedback Mapping
Never call native haptics directly. Always use the cross-platform abstraction in:
```typescript
import { lightImpact } from "@/platform/haptics";
```

### Standard Interaction Haptic Matrix:

| User Action | Haptic Style | Standard Trigger |
| :--- | :--- | :--- |
| **Primary Button Press / CTA** | `lightImpact()` | On press release (`onPress`) before async action begins |
| **Tab / Radio Selection** | `selectionChanged()` | On state change confirmation |
| **Pull-to-Refresh Trigger** | `mediumImpact()` | At the exact moment the refresh threshold is crossed |
| **Swipe Action Threshold (Archive/Delete)** | `heavyImpact()` | When the swipe reaches the trigger activation point |
| **Error / Validation Shake** | `notificationError()` | On invalid form submission or network rejection |

---

## 2. Gesture Handling & Physics (Reanimated + GestureHandler)
- **Engine**: Always use `react-native-gesture-handler` (version 2+) and `react-native-reanimated` worklets.
- **Forbidden**: Never use legacy `PanResponder` or basic CSS keyframe transitions that drop frames on the JavaScript thread.
- **Spring Physics Standard**:
  - All interactive springs (swipe dismissal, sheet snap, card flick) MUST use standard damping and stiffness:
  ```typescript
  withSpring(targetValue, {
    damping: 18,
    stiffness: 140,
    mass: 0.8,
  });
  ```

---

## 3. Touch Ripple & Press States
- **Standard Pressed Opacity**: All interactive touch surfaces must provide immediate visual feedback.
  - Active press opacity: `opacity: pressed ? 0.72 : 1`
  - Disabled opacity: `opacity: 0.45`
- **Hit Slop**: Small interactive touch targets (<44x44px) must define:
  ```typescript
  hitSlop={{ top: 10, bottom: 10, left: 10, right: 10 }}
  ```

---

## 4. Keyboard Dismissal & Focus Management
- Interactive forms or chat screens must handle keyboard avoidance using `KeyboardAvoidingView` with `behavior={Platform.OS === "ios" ? "padding" : "height"}`.
- Tapping outside an input area in any form view must dismiss the keyboard (`Keyboard.dismiss`).
