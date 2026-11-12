"""Compact strip chart for the test bench panel.

Each graph owns its own Y axis with round tick steps; series of another unit
can go on a second axis on the right. Series are fixed-rate ring buffers
filled by the panel; the graph only draws the newest samples that fit the
selected time window.
"""

import math

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk


def nice_range(lo, hi, ticks=4):
    """Return (lo, hi, step) rounded to a 1/2/2.5/5/10 step."""
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / ticks
    power = 10 ** math.floor(math.log10(raw))
    for mult in (1, 2, 2.5, 5, 10):
        step = mult * power
        if raw <= step:
            break
    return math.floor(lo / step) * step, math.ceil(hi / step) * step, step


def step_decimals(step):
    """Decimals that show every tick of a 1/2/2.5/5 step exactly."""
    if float(step).is_integer():
        return 0
    power = math.floor(math.log10(step) + 1e-9)
    mantissa = round(step / 10**power, 6)
    return max(0, -power) + (1 if mantissa == 2.5 else 0)


def hex_to_rgb(color):
    color = color.lstrip("#")
    return tuple(int(color[i : i + 2], 16) / 255 for i in (0, 2, 4))


class Axis:
    def __init__(self, zero_based, min_span, decimals=None, rgb=None):
        self.zero_based = zero_based
        self.min_span = min_span
        self.decimals = decimals
        self.rgb = rgb
        self.lo, self.hi, self.step = 0.0, 1.0, 1.0

    def fit(self, values):
        if values:
            lo, hi = min(values), max(values)
        else:
            lo, hi = 0.0, self.min_span
        if self.zero_based:
            lo = min(0.0, lo)
            hi = max(hi * 1.1, lo + self.min_span)
        else:
            mid = (lo + hi) / 2
            half = max((hi - lo) / 2 * 1.2, self.min_span / 2)
            lo, hi = mid - half, mid + half
        self.lo, self.hi, self.step = nice_range(lo, hi)

    def ticks(self):
        decimals = self.decimals if self.decimals is not None else step_decimals(self.step)
        result = []
        for i in range(round((self.hi - self.lo) / self.step) + 1):
            value = self.lo + i * self.step
            if abs(value) < self.step * 1e-6:
                value = 0.0
            result.append((value, f"{value:.{decimals}f}"))
        return result

    def y(self, value, top, bottom):
        return bottom - (bottom - top) * (value - self.lo) / (self.hi - self.lo)


class BenchGraph(Gtk.DrawingArea):
    def __init__(self, sample_rate, font_px, zero_based=True, min_span=1.0, decimals=None):
        super().__init__(hexpand=True, vexpand=True)
        self.sample_rate = sample_rate
        self.font_px = font_px
        self.axis = Axis(zero_based, min_span, decimals)
        self.right_axis = None
        self.window_s = 120
        self.series = []  # (buffer, rgb, dashed, on the right axis)
        self.message = None
        self.connect("draw", self.on_draw)

    def add_series(self, buffer, color, dashed=False, right=False):
        self.series.append((buffer, hex_to_rgb(color), dashed, right))

    def set_right_axis(self, color, zero_based=False, min_span=1.0, decimals=None):
        """Second Y axis for the series added with right=True, drawn while they have data."""
        self.right_axis = Axis(zero_based, min_span, decimals, hex_to_rgb(color))

    def set_window(self, seconds):
        self.window_s = seconds
        self.queue_draw()

    def _visible(self, buffer):
        count = int(self.window_s * self.sample_rate)
        data = list(buffer)
        return data[-count:] if len(data) > count else data

    def on_draw(self, widget, ctx):
        width = self.get_allocated_width()
        height = self.get_allocated_height()
        fs = self.font_px
        ctx.select_font_face("monospace")
        ctx.set_font_size(fs)
        ctx.set_line_width(1)

        visible = [(self._visible(buf), rgb, dashed, r) for buf, rgb, dashed, r in self.series]
        left_values = [y for data, _, _, r in visible if not r for y in data if y is not None]
        right_values = [y for data, _, _, r in visible if r for y in data if y is not None]
        self.axis.fit(left_values)
        right_axis = self.right_axis if self.right_axis is not None and right_values else None
        right_ticks = []
        if right_axis is not None:
            right_axis.fit(right_values)
            right_ticks = right_axis.ticks()
        right_w = max((ctx.text_extents(t).x_advance for _, t in right_ticks), default=0)

        left, top, bottom = fs * 3.6, fs * 0.6, height - fs * 1.4
        right = width - fs * 0.6 - (right_w + fs * 0.5 if right_axis else 0)
        plot_h = bottom - top
        if plot_h < 10 or right - left < 10:
            return

        # Horizontal gridlines and tick labels of the left axis
        for value, label in self.axis.ticks():
            y = self.axis.y(value, top, bottom)
            ctx.set_source_rgb(0.15, 0.18, 0.21)
            ctx.move_to(left, y)
            ctx.line_to(right, y)
            ctx.stroke()
            ext = ctx.text_extents(label)
            ctx.set_source_rgb(0.54, 0.57, 0.61)
            ctx.move_to(left - ext.width - fs * 0.4, y + ext.height / 2)
            ctx.show_text(label)

        # Axes
        ctx.set_source_rgb(0.29, 0.33, 0.38)
        ctx.move_to(left, top)
        ctx.line_to(left, bottom)
        ctx.line_to(right, bottom)
        ctx.stroke()

        # Right axis: line, ticks and labels in the color of its series
        if right_axis is not None:
            ctx.set_source_rgb(*right_axis.rgb)
            ctx.move_to(right, top)
            ctx.line_to(right, bottom)
            for value, label in right_ticks:
                y = right_axis.y(value, top, bottom)
                ctx.move_to(right - fs * 0.3, y)
                ctx.line_to(right, y)
            ctx.stroke()
            for value, label in right_ticks:
                y = right_axis.y(value, top, bottom)
                ext = ctx.text_extents(label)
                ctx.move_to(right + fs * 0.3, y + ext.height / 2)
                ctx.show_text(label)

        # Time labels: -window ... 0 s
        ctx.set_source_rgb(0.42, 0.45, 0.50)
        for i in range(5):
            x = left + (right - left) * i / 4
            label = "0 s" if i == 4 else f"-{round(self.window_s * (4 - i) / 4)}"
            ext = ctx.text_extents(label)
            x = min(max(x - ext.width / 2, left), right - ext.width)
            ctx.move_to(x, height - fs * 0.25)
            ctx.show_text(label)

        if self.message:
            ext = ctx.text_extents(self.message)
            ctx.set_source_rgb(0.42, 0.45, 0.50)
            ctx.move_to(left + (right - left - ext.width) / 2, top + plot_h / 2)
            ctx.show_text(self.message)

        # Series, right-aligned so the newest sample is at 0 s
        count = int(self.window_s * self.sample_rate)
        x_step = (right - left) / max(count - 1, 1)
        ctx.save()
        ctx.rectangle(left, top, right - left, plot_h)
        ctx.clip()
        ctx.set_line_width(max(1.5, fs / 8))
        for data, rgb, dashed, on_right in visible:
            if not data:
                continue
            axis = right_axis if on_right else self.axis
            if axis is None:
                continue
            ctx.set_source_rgb(*rgb)
            ctx.set_dash([fs * 0.6, fs * 0.4] if dashed else [])
            offset = count - len(data)
            pixels = right - left
            if len(data) > pixels * 2:
                # Min/max envelope per pixel keeps short spikes visible
                per_px = len(data) / pixels
                started = False
                for px in range(int(pixels)):
                    chunk = data[int(px * per_px) : int((px + 1) * per_px) or None]
                    chunk = [v for v in chunk if v is not None]
                    if not chunk:
                        started = False
                        continue
                    x = left + (offset * x_step) + px * (len(data) * x_step / pixels)
                    for v in (min(chunk), max(chunk)):
                        y = axis.y(v, top, bottom)
                        if started:
                            ctx.line_to(x, y)
                        else:
                            ctx.move_to(x, y)
                            started = True
            else:
                started = False
                for i, v in enumerate(data):
                    if v is None:
                        started = False
                        continue
                    x = left + (offset + i) * x_step
                    y = axis.y(v, top, bottom)
                    if started:
                        ctx.line_to(x, y)
                    else:
                        ctx.move_to(x, y)
                        started = True
            ctx.stroke()
        ctx.restore()
