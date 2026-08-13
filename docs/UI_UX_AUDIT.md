# Ledgerly UI/UX Adversarial Audit

Date: 2026-08-01
Scope: Android Compose client, static review plus focused JVM/build validation. Real-device notification testing remains required.

## What is working well

- The calm-dark palette has explicit Material 3 roles and semantic income, expense, warning, and chart colors.
- Amounts use tabular numerals, IDR previews update while typing, and forms use a timezone-safe date picker.
- Manual entry and notification review dialogs scroll and apply IME padding, so the primary capture flow remains usable on compact screens.
- Transaction rows expose an accessible action description; status and error messages use live-region semantics.
- The five-destination navigation model is easy to reach with one hand, and the selected destination retains its label.

## Findings and actions

### Fixed — Diagnostics had no visible back affordance (high)

Opening diagnostics hides the bottom navigation. Previously the only exit was the system back gesture, which is discoverability and accessibility debt. The screen now has a labeled back button and `diagnostics_back` test tag.

### Fixed — Budget month was presented as an ISO implementation value (medium)

`2026-08` exposed storage formatting instead of a user-facing month. It now renders as `August 2026`; the API still receives `YYYY-MM`.

### Fixed — Budget editor could be obscured by the keyboard (medium)

The editor now scrolls and applies IME padding, matching manual and notification-review forms.

### Follow-up — language consistency (medium)

The app is English while common capture messages and bank labels are Indonesian. Choose one primary language before wider rollout; avoid mixed labels in the same flow.

### Follow-up — narrow-screen and large-font pass (medium)

Validate 320dp width, landscape, and Android large-font settings on a physical device. Pay particular attention to navigation labels, long merchant names, diagnostics rows, and the transaction filter chips.

### Follow-up — visual regression coverage (low)

Add screenshot tests for dashboard, inbox review, transaction detail, budgets, and diagnostics once an emulator or device is available. The current UI tests cover interaction semantics, not visual drift.

## Release gate

Before sideloading a release APK, run focused unit tests, assemble the debug/release artifact, then validate notification access and one BSI, Mandiri, and Jago capture on the target Android device.
