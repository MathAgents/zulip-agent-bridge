"""The ten-cell emoji bar shared by `!usage` and `!vm` (SPEC §4.6; issue #204).

One function for both, so the cells, thresholds and colours always agree: 🟩 below 70%,
🟨 below 80%, 🟧 below 90%, 🟥 at 90% or more. The colour and the filled cells follow the whole
percent shown next to the bar, so a bar labelled 70% is never green and a bar labelled 35%
always has four cells.
"""

from __future__ import annotations

import math

CELLS = 10


def whole_percent(fraction: float) -> int:
    """0.125 -> 13: half up, clamped to 0..100."""
    return math.floor(max(0.0, min(1.0, fraction)) * 100 + 0.5)


def cells(fraction: float) -> str:
    """Ten cells, filled to the nearest tenth of the whole percent (halves up); any use above
    zero fills at least one."""
    percent = whole_percent(fraction)
    color = "🟩" if percent < 70 else "🟨" if percent < 80 else "🟧" if percent < 90 else "🟥"
    filled = max(1 if fraction > 0 else 0, (percent + 5) // 10)
    return color * filled + "⬜" * (CELLS - filled)


def bar(fraction: float) -> str:
    """`🟩🟩⬜⬜⬜⬜⬜⬜⬜⬜ 18%`: the cells and the whole percent."""
    return f"{cells(fraction)} {whole_percent(fraction)}%"
