"""Charts drawn on the server as SVG: no chart library, and nothing for the CSP to block
(sizes are SVG attributes, not inline styles).

One series per chart, so one colour (--chart-bar: validated with the dataviz
validator against both surfaces: #b45309 light, #cc7f1a dark), no legend
(the title names it), a recessive grid, thin bars with a 2px gap and rounded
data-ends sitting on the baseline. Each bar has a <title> (a hover label),
and every chart has a table view on the page.
"""
import math
from dataclasses import dataclass

WIDTH, HEIGHT = 640, 250
LEFT, RIGHT, TOP, BOTTOM = 48, 10, 14, 34
GAP = 2
RADIUS = 4
CHAR_WIDTH = 8.2  # about how wide one character of an axis label is, at 14px


def nice_max(value):
    """A round number at or above value for the axis top: 1, 2, 5, 10, 20, 50..."""
    if value <= 0:
        return 1
    power = 10 ** math.floor(math.log10(value))
    for step in (1, 2, 5, 10):
        if value <= step * power:
            return step * power
    return 10 * power


def bar_path(x, y, width, base):
    """A bar with its top corners rounded (the data end) and a flat bottom on the baseline."""
    r = min(RADIUS, width / 2, max(0.0, base - y))
    return (f"M{x:.1f},{base:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
            f"H{x + width - r:.1f} Q{x + width:.1f},{y:.1f} {x + width:.1f},{y + r:.1f} V{base:.1f} Z")


@dataclass
class Bar:
    path: str
    label: str
    value: int
    title: str
    x_mid: float


def columns(points):
    """points: [(label, value, hover title)] -> everything the template draws."""
    top = nice_max(max((value for _label, value, _title in points), default=0))
    plot_w, plot_h = WIDTH - LEFT - RIGHT, HEIGHT - TOP - BOTTOM
    base = TOP + plot_h
    slot = plot_w / max(len(points), 1)
    width = max(1.0, slot - GAP)
    bars = []
    for index, (label, value, title) in enumerate(points):
        x = LEFT + index * slot + GAP / 2
        y = base - plot_h * value / top
        bars.append(Bar(bar_path(x, y, width, base) if value else "", label, value, title, x + width / 2))
    # Whole, evenly spaced ticks: tops are 1, 2, 5, 10, 20, 50..., so every 1 up to 5, else fifths.
    steps = top if top <= 5 else 5
    ticks = [(round(base - plot_h * i / steps, 1), round(top * i / steps)) for i in range(steps + 1)]
    # Show only as many x labels as fit side by side, always including the last.
    longest = max((len(label) for label, _v, _t in points), default=1) * CHAR_WIDTH + 12
    every = max(1, math.ceil(longest / slot))
    shown = {i for i in range(len(points)) if (len(points) - 1 - i) % every == 0}
    return {"bars": bars, "ticks": ticks, "base": base, "left": LEFT, "right": WIDTH - RIGHT,
            "width": WIDTH, "height": HEIGHT, "shown": shown, "label_y": base + 22,
            "empty": not any(value for _label, value, _title in points)}


def funnel(steps):
    """steps: [(label, value)] -> horizontal bars for the delivery funnel, widths in % of the first."""
    first = steps[0][1] if steps and steps[0][1] else 0
    rows = []
    for label, value in steps:
        share = 100 * value / first if first else 0
        rows.append({"label": label, "value": value, "share": share, "width": round(share, 2)})
    return rows
