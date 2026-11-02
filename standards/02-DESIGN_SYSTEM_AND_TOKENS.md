# 02 — Design System & UI Tokens Standards — yuna-app

> **MANDATORY CONTRACT**: Every SSDLC ticket touching UI/UX in `client/yuna-app` MUST strictly adhere to these design tokens and patterns. Hardcoded hex colors and magic number spacing/radii are strictly prohibited and will be rejected at the Code Review gate.

---

## 1. Import Path
Always import the base tokens and theme hook directly from:
```typescript
import { space, shape, type, useTheme } from "@/theme/tokens";
// If working in Cosmic Dark experience:
import { cosmicColors, cosmicGradient, cosmicShadows, neonGlowStyle } from "@/theme/tokens";
```

---

## 2. Spacing Scale (`space`)
Never use arbitrary margin/padding values (e.g. `12`, `15`, `7`). Use only the standard scale:

| Token | Value | Intended Usage |
| :--- | :--- | :--- |
| `space.xs` | `4px` | Micro-spacing, badge padding, icon-text gap |
| `space.sm` | `8px` | Inner component padding, small card gap |
| `space.md` | `16px` | Standard screen horizontal padding, card padding |
| `space.lg` | `24px` | Section separation, large gaps |
| `space.xl` | `32px` | Hero section padding, modal margins |

---

## 3. Border Radii (`shape`)
Never use arbitrary `borderRadius`. Use only:

| Token | Value | Intended Usage |
| :--- | :--- | :--- |
| `shape.sm` | `8px` | Small tags, badges, input fields |
| `shape.md` | `14px` | Standard cards, action buttons, dialogs |
| `shape.lg` | `22px` | Large modals, floating action containers |
| `shape.pill` | `999px` | Fully rounded pills, avatar circles, filter chips |

---

## 4. Typography Scale (`type`)
Never use arbitrary font sizes. Use:

| Token | Size | Weight / Usage |
| :--- | :--- | :--- |
| `type.small` | `13px` | Captions, timestamps, secondary labels |
| `type.body` | `16px` | Standard body text, button labels, inputs |
| `type.title` | `24px` | Screen titles, card headers |
| `type.hero` | `34px` | Landing headlines, hero numbers |

---

## 5. Color Palette & Dynamic Theme (`useTheme()`)
Always retrieve colors dynamically via `const theme = useTheme();`:

| Property | Light Theme | Dark Theme | Purpose |
| :--- | :--- | :--- | :--- |
| `theme.color.canvas` | `#F6F7F9` | `#0E1520` | Root screen background |
| `theme.color.surface` | `#FFFFFF` | `#162231` | Card and modal surfaces |
| `theme.color.surfaceRaised` | `#ECF1F3` | `#203044` | Elevated cards, secondary buttons |
| `theme.color.text` | `#16202A` | `#F7F4ED` | Primary readable text |
| `theme.color.muted` | `#536271` | `#B4C2CC` | Secondary labels, hints |
| `theme.color.border` | `#D6DEE3` | `#35475A` | Hairline dividers and card borders |
| `theme.color.primary` | `#087F78` | `#42C8BA` | Primary brand accent / CTA buttons |
| `theme.color.primaryText` | `#FFFFFF` | `#08120F` | Text on top of primary button |
| `theme.color.danger` | `#B42318` | `#FF8A80` | Errors, deletion, destructive actions |
| `theme.color.warning` | `#9A6700` | `#F7C968` | Warning badges, notices |
| `theme.color.success` | `#067647` | `#75E0A7` | Positive indicators, badges |

---

## 6. Safe Area & Viewport Rule
- **Mandatory Safe Area**: Screens and floating footers must use `useSafeAreaInsets` from `react-native-safe-area-context`:
  ```typescript
  import { useSafeAreaInsets } from "react-native-safe-area-context";
  const insets = useSafeAreaInsets();
  ```
- **Prohibited**: Never use fixed top padding (e.g. `paddingTop: 16`) for root screen headers.

---

## 7. Icons & Visual Assets Rule
- **Strictly Prohibited**: Raw Unicode characters or emoji (e.g. `♥`, `★`, `⚙`, `✕`) as icon substitutes in buttons, navigation bars, or tabs.
- **Permitted**: Use approved image assets from `@/assets/` or vector icon support.
