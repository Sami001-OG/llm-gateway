# Design system — Gateway Control Center

## Mode
**Operate** (admin dashboard / tool). The interface disappears into the task:
scanability, consistent affordances, dense but calm data. Brand appears in
precise details, not expression. No marketing patterns, no page-load choreography.

## Palette — restrained
One accent, tinted neutrals. Defined in OKLCH so chroma stays consistent across
lightness steps; no pure black or white.

| Token | Value | Role |
| --- | --- | --- |
| `--bg` | `oklch(0.155 0.014 245)` | app canvas, cool-tinted slate |
| `--bg-elev` | `oklch(0.200 0.018 245)` | sidebar, inputs, table header |
| `--surface` | `oklch(0.225 0.020 245)` | cards, panels |
| `--surface-2` | `oklch(0.255 0.021 245)` | hover rows / buttons |
| `--border` | `oklch(0.325 0.020 245)` | 1px structure |
| `--text` | `oklch(0.965 0.008 245)` | primary |
| `--muted` | `oklch(0.680 0.016 245)` | secondary, ≥4.5:1 |
| `--accent` | `oklch(0.845 0.125 168)` | primary actions, selection, state only |
| `--ok / --warn / --danger` | green / amber / red tints | status vocabulary |

No violet or lavender. Accent is used for actions, current selection and state —
never decoration.

## Type
One family (system sans), fixed rem scale, tight ratio. 10.5px uppercase labels,
12px body/table, 14px card headings, 20px page title. `font-variant-numeric:
tabular-nums` on all counters and numeric table columns. Tracking floor
`-0.03em` on large numbers only.

## Layout
Sidebar 236px → 64px icon rail below 820px → content collapses to one column at
560px. Equal left/right content edges (34px desktop, 18px mobile). One visual
system for every table, button, chip, badge and form control.

## Motion
- Only `transform` and `opacity` animate. No `transition: all`, no layout props.
- `--ease-out: cubic-bezier(0.23, 1, 0.32, 1)` for entrances and hover; no
  `ease-in` anywhere.
- Press feedback: `scale(0.97)` in 150ms on every pressable element.
- Modal and toast enter from `scale(0.96)` / `translateY(12px)` + opacity —
  never `scale(0)`. Exit is faster than enter. Modal origin stays centered;
  popovers would be origin-aware (none currently used).
- Buttons step on hover (`translateY(-1px)`), press on click.
- **No animation on navigation/tab switching** — it happens constantly.
- One authored moment only: the modal entrance. Toasts use a calm 400ms ease.
- `prefers-reduced-motion`: motion removed, color/opacity feedback retained.

## States
Every interactive element has default, hover, focus-visible, active/pressed,
disabled, loading (busy button) and error (toast + inline message) states.
Empty states teach what to do next. Status badges are a single vocabulary:
Ready / Cooling Ns / Disabled.

## Browser surfaces
`::selection`, `caret-color`, focus ring, scrollbar-adjacent surfaces and
tabular numerals are themed from the palette — these details are part of the
system, not browser defaults.

## Verification
Rendered and exercised in a real browser at 1440, 768 and 390 via Playwright:
login, all six views, add-account / quick-add-provider / route / token modals,
Escape-to-close, and horizontal-overflow checks. Zero console errors. Screenshots
live in the tool output directory during verification and are not committed.
