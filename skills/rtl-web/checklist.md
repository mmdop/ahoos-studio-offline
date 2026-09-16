# Before returning RTL work

Check each of these against what you actually wrote:

- `dir` is set once, on the root, and nothing overrides it further down.
- Search your own stylesheet for these exact strings before you return it. Every
  one of them is a bug in RTL, and each has a logical replacement:

      margin-left      -> margin-inline-start
      margin-right     -> margin-inline-end
      padding-left     -> padding-inline-start
      padding-right    -> padding-inline-end
      left:            -> inset-inline-start:
      right:           -> inset-inline-end:
      border-left      -> border-inline-start
      border-right     -> border-inline-end
      text-align: left -> text-align: start

  This is the single most common way RTL work fails review. Shorthand like
  `margin: 0 0 0 1rem` hides the same bug — use `margin-inline-start` instead.
- No `flex-direction: row-reverse` was added to compensate for direction.
- Directional icons mirror; semantic icons and logos do not.
- Latin technical strings embedded in RTL text carry `dir="ltr"`.
- Focus order follows visual order after mirroring.
- Any horizontal scroll container starts at the correct edge.

State in one line which of these you verified and which you could not, since you
cannot render the page.
