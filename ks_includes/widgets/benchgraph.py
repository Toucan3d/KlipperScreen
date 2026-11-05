"""Compact strip chart for the test bench panel.

Each graph owns its own Y axis with round tick steps. Series are fixed-rate
ring buffers filled by the panel; the graph only draws the newest samples
that fit the selected time window.
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


def hex_to_rgb(color):
    color = color.lstrip("#")
    return tuple(int(color[i : i + 2], 16) / 255 for i in (0, 2, 4))


class BenchGraph(Gtk.DrawingArea):
    def __init__(self, sample_rate, font_px, zero_based=True, min_span=1.0, decimals=None):
        super().__init__(hexpand=True, vexpand=True)
        self.sample_rate = sample_rate
        self.font_px = font_px
        self.zero_based = zero_based
        self.min_span = min_span
        self.decimals = decimals
        self.window_s = 120
        self.series = []  # (buffer, rgb)
        self.message = None
        self.connect("draw", self.on_draw)

    def add_series(self, buffer, color):
        self.series.append((buffer, hex_to_rgb(color)))

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
        left, right, top, bottom = fs * 3.6, width - fs * 0.6, fs * 0.6, height - fs * 1.4
        plot_h = bottom - top
        if plot_h < 10 or right - left < 10:
            return

        visible = [(self._visible(buf), rgb) for buf, rgb in self.series]
        values = [y for data, _ in visible for y in data if y is not None]
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
        lo, hi, step = nice_range(lo, hi)
        span = hi - lo

        ctx.select_font_face("monospace")
        ctx.set_font_size(fs)
        ctx.set_line_width(1)

        # Horizontal gridlines and Y tick labels
        decimals = self.decimals
        if decimals is None:
            decimals = 0 if float(step).is_integer() else (1 if step >= 0.1 else 2)
        value = lo
        while value <= hi + step / 2:
            y = bottom - plot_h * (value - lo) / span
            ctx.set_source_rgb(0.15, 0.18, 0.21)
            ctx.move_to(left, y)
            ctx.line_to(right, y)
            ctx.stroke()
            label = f"{value:.{decimals}f}"
            ext = ctx.text_extents(label)
            ctx.set_source_rgb(0.54, 0.57, 0.61)
            ctx.move_to(left - ext.width - fs * 0.4, y + ext.height / 2)
            ctx.show_text(label)
            value += step

        # Axes
        ctx.set_source_rgb(0.29, 0.33, 0.38)
        ctx.move_to(left, top)
        ctx.line_to(left, bottom)
        ctx.line_to(right, bottom)
        ctx.stroke()

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
        for data, rgb in visible:
            if not data:
                continue
            ctx.set_source_rgb(*rgb)
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
                        y = bottom - plot_h * (v - lo) / span
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
                    y = bottom - plot_h * (v - lo) / span
                    if started:
                        ctx.line_to(x, y)
                    else:
                        ctx.move_to(x, y)
                        started = True
            ctx.stroke()
        ctx.restore()
