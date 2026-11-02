# Living Component Catalog — yuna-app

> **REUSE FIRST POLICY**: Before creating any new UI component, the Developer agent MUST inspect this catalog and reuse existing components. If a new reusable component is created in a ticket, it will be added to this catalog upon supervisor merge.

---

## Existing UI Primitives (`@/components/Primitives`)

### 1. `<Button>`
- **Source**: `client/yuna-app/src/components/Primitives.tsx`
- **Props**:
  - `label: string`
  - `onPress: () => void`
  - `kind?: "primary" | "secondary" | "danger"` (default: `"primary"`)
  - `disabled?: boolean`
  - `busy?: boolean` (disables and shows loading behavior)
  - `selected?: boolean` (for radio/tab selection)
- **Features**: Automatic theme styling, accessible labels, built-in haptic feedback (`lightImpact()`).

### 2. `<Card>`
- **Source**: `client/yuna-app/src/components/Primitives.tsx`
- **Props**: `style?: ViewStyle`, `children: React.ReactNode`
- **Features**: Automatically styled with `theme.color.surface`, `theme.color.border`, and `shape.md`.

### 3. `<Loading>`
- **Source**: `client/yuna-app/src/components/Primitives.tsx`
- **Props**: `label?: string` (defaults to localized `loadingYuna`)
- **Features**: Standard `ActivityIndicator` styled with `theme.color.primary`.

### 4. `<Wordmark>`
- **Source**: `client/yuna-app/src/components/Primitives.tsx`
- **Features**: Header branded logo with `icon.png` and theme-reactive Yuna text.

---

## Specialized Components

### 5. `<SecureImage>`
- **Source**: `client/yuna-app/src/components/SecureImage.tsx`
- **Props**: `uri?: string`, `name: string`, `accessibilityLabel: string`
- **Features**: Validates URLs against allowed CDN patterns, renders cosmic fallback initials if invalid.

### 6. `<RatingStars>`
- **Source**: `client/yuna-app/src/components/RatingStars.tsx`
- **Features**: Accessible star rating display for products and reviews.

### 7. `<UpgradeBanner>`
- **Source**: `client/yuna-app/src/components/UpgradeBanner.tsx`
- **Features**: Callout card for premium features and tiers.
