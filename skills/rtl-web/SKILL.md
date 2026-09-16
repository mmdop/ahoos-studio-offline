# Right-to-left first

Build for right-to-left from the start. Direction is a layout property, not a
translation step, and retrofitting it into a left-to-right layout produces a UI that
is technically mirrored and practically broken.

Set `dir` on the root element and let it cascade. Never set direction per-component
and never override it with CSS `direction` on individual elements — that is how a
layout ends up with islands pointing the wrong way.

Use logical properties everywhere. `margin-inline-start`, not `margin-left`.
`padding-inline`, not `padding-left`/`padding-right`. `inset-inline-start`, not
`left`. `border-inline-end`, not `border-right`. `text-align: start`, not `left`.
These flip automatically with direction; their physical counterparts do not.

Flexbox and grid already follow direction — `flex-direction: row` reverses on its
own. Do not add `row-reverse` to "fix" RTL; that double-flips it back.

Some things stay physical and must not flip: media playback controls, progress that
tracks real time, code blocks, phone numbers, URLs, and version strings. Latin
technical strings inside RTL text need `dir="ltr"` on their own element or they
fragment at punctuation.

Numerals, dates, and currency follow the locale, not the direction. Do not assume a
numeral system from the fact that the page is RTL.

Icons that carry direction — arrows, chevrons, undo, next/previous — must mirror.
Icons that carry meaning — a clock, a checkmark, a logo — must not.

Mirror the whole shell, not just the text: navigation, sidebars, drawer entry
direction, the side scrollbars sit on, the origin corner of shadows and slide
animations.
