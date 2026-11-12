"""Test bench dashboard.

Home screen for a Klipper test bench instead of a printer. Enabled by the
"Test bench layout" option in the KlipperScreen settings; the stock main menu
stays reachable from the "Menu" button.

Left: equal-height live graphs (flow, temperatures, optional nozzle force
with the eddy-current sensor on its own right axis).
Right: data grid, print status (only while printing), console, favorite
buttons (macros FAV_1..FAV_7, defined and labelled in the Klipper config),
flow monitor buttons, nozzle buttons (extrude, retract, heat) and fan
sliders.

Every label ellipsizes, so long sensor names never widen the columns past
the screen.
"""

import logging
import re
import time
from collections import deque
from datetime import datetime

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango

from ks_includes.bench_eddy import EddyTail
from ks_includes.screen_panel import ScreenPanel
from ks_includes.widgets.benchgraph import BenchGraph

SAMPLE_HZ = 4
WINDOWS = (30, 120, 600)
FAV_COUNT = 7
CONSOLE_LINES = 200
FLOW_COLOR = "#5DCAA5"
FLOW_SET_COLOR = "#C9CCD1"  # expected flow from the extruder move, dashed
FORCE_COLOR = "#97C459"
EDDY_COLOR = "#85B7EB"
# Live CSVs of the testbench eddy recorder; option bench_eddy_glob, comma separated, empty = off
EDDY_GLOB = "~/testbench/runs/*/logs/eddy*.csv"
EDDY_MIN_SPAN_KHZ = 0.02  # the sensor noise (a few Hz) does not fill the axis
TEMP_COLORS = ("#ED93B1", "#AFA9EC", "#FAC775", "#85B7EB", "#F0997B")
CONSOLE_COLORS = {
    "command": "#85B7EB",
    "error": "#F09595",
    "response": "#C9CCD1",
    "info": "#8A929C",
    "time": "#6B7480",
}
# Nozzle buttons; each can be overridden in KlipperScreen.conf [main]
NOZZLE_DEFAULTS = {
    "bench_extrude_mm": 50.0,       # length of one extrude / retract
    "bench_extrude_speed": 2.0,     # mm/s
    "bench_heat_temp": 250.0,       # target of the heat button
    "bench_heat_timeout": 10.0,     # minutes until the heat button switches off again
}
MOVE_STEP_MM = 5.0  # moves are sent in steps so Stop takes effect quickly
TEMP_REPORT = re.compile(r"^(?:ok\s+)?(B|C|T\d*):")

# Stock Klipper sensor types that measure a real temperature (lower case).
# Custom [thermistor x] / [adc_temperature x] types are added at runtime.
TEMPERATURE_TYPES = {
    # klippy/extras/temperature_sensors.cfg
    "atc semitec 104gt-2", "atc semitec 104nt-4-r025h42g", "epcos 100k b57560g104f",
    "generic 3950", "sliceengineering 450", "tdk ntcg104lh104jt1",
    "honeywell 100k 135-104lag-j01", "ntc 100k mgb18-104f39050l32",
    # adc_temperature.py
    "ad595", "ad597", "ad8494", "ad8495", "ad8496", "ad8497", "pt100 ina826", "pt1000",
    # spi_temperature.py
    "max6675", "max31855", "max31856", "max31865",
    # i2c / 1-wire / internal sensors
    "aht10", "aht1x", "aht2x", "aht3x", "bme280", "bmp280", "bme680", "bmp180", "bmp388",
    "ds18b20", "htu21d", "sht21", "si7013", "si7020", "si7021", "sht3x", "lm75", "mlx90614",
    "temperature_mcu", "temperature_host", "temperature_combined", "temperature_hcu",
}

CSS = """
.bench {{ font-size: {base}px; }}
.bench_mono {{ font-family: monospace; font-size: {small}px; }}
.bench_data label {{ font-family: monospace; font-size: {tiny}px; }}
.bench_box {{ background-color: #181d24; border-radius: 6px; }}
.bench_data {{ background-color: #1a2028; border-radius: 6px; padding: 2px 6px; }}
.bench_console {{ background-color: #0d1014; border-radius: 6px; }}
.bench_console textview, .bench_console textview text {{
    background-color: #0d1014; font-family: monospace; font-size: {small}px; }}
.bench_console entry {{ font-family: monospace; font-size: {small}px; min-height: 0;
    padding: 2px 6px; margin: 2px; }}
button.bench_btn {{ background-color: #262e38; border: 1px solid #3a4553; border-radius: 6px;
    margin: 0; padding: 2px; font-size: {base}px; min-height: 0; min-width: 0; color: #e8eaed; }}
button.bench_btn label {{ font-size: {base}px; }}
button.bench_btn:active {{ background-color: #3a4553; }}
button.bench_go {{ background-color: #0F6E56; border-color: #1D9E75; }}
button.bench_heat {{ background-color: #854F0B; border-color: #BA7517; }}
button.bench_stop {{ background-color: #791F1F; border-color: #A32D2D; }}
button.bench_unset {{ color: #6b7480; }}
.bench_print {{ background-color: #1a2028; border-radius: 6px; padding: 0 4px; }}
.bench_fan {{ background-color: #1a2028; border-radius: 6px; padding: 0 6px; }}
.bench_fan scale {{ padding: 0; margin: 0 4px; min-height: 0; }}
.bench_fan scale trough {{ min-height: 6px; padding: 0; margin: 0; border-radius: 3px; }}
.bench_fan scale highlight {{ min-height: 6px; background-color: #378ADD; }}
.bench_fan scale slider {{ min-width: {knob}px; min-height: {knob}px; margin: -{knobm}px;
    padding: 0; border-radius: {knob}px; background-color: #e8eaed; }}
"""


def fmt(value, decimals, unit=""):
    if value is None:
        return "—"
    try:
        text = f"{float(value):.{decimals}f}"
    except (TypeError, ValueError):
        return str(value)
    return f"{text} {unit}" if unit else text


class Panel(ScreenPanel):
    css_provider = None

    def __init__(self, screen, title):
        title = title or _("Bench")
        # KlipperScreen reinitializes panels by calling __init__ again on the
        # same object, so stop the timers of the previous run first.
        for attr in ("sample_timer", "draw_timer", "move_timer"):
            if getattr(self, attr, None) is not None:
                GLib.source_remove(getattr(self, attr))
        self.sample_timer = self.draw_timer = self.move_timer = None
        super().__init__(screen, title)
        fs = self._gtk.font_size
        self.base_px = max(11, round(fs * 0.66))
        self.small_px = max(10, round(fs * 0.56))
        self.tiny_px = max(10, round(fs * 0.5))
        self._install_css()

        self.encoder = self._first_section("filament_encoder ")
        self.encoder_name = self.encoder.split(" ", 1)[1] if self.encoder else None
        self.load_cell = self._first_section("load_cell")
        self.show_force = self._config.get_main_config().getboolean("bench_force_graph", True)
        self.temps = self._temperature_objects()
        self.fans = [
            f for f in self._printer.get_fans() if f == "fan" or f.startswith("fan_generic ")
        ]
        sensors = self._printer.get_filament_sensors()
        self.runout = sensors[0] if sensors else None
        tools = [t for t in self._printer.get_tools() if t.startswith("extruder")]
        self.extruder = tools[0] if tools else None
        cfg = (self._printer.get_config_section(self.extruder) or {}) if self.extruder else {}
        self.min_extrude_temp = float(cfg.get("min_extrude_temp", 170))
        diameter = float(cfg.get("filament_diameter", 1.75))
        self.filament_area = 3.14159265 * diameter * diameter / 4
        main = self._config.get_main_config()
        self.nozzle = {k: main.getfloat(k, fallback=v) for k, v in NOZZLE_DEFAULTS.items()}
        self.move = None
        self.heat_since = None
        # Eddy-current force sensor: not a Klipper object, followed in the recorder's CSV
        pattern = main.get("bench_eddy_glob", fallback=EDDY_GLOB)
        self.eddy = EddyTail(pattern) if self.show_force and pattern.strip() else None
        self.eddy_failed = False

        size = max(WINDOWS) * SAMPLE_HZ
        self.buffers = {"flow": deque(maxlen=size), "flow_set": deque(maxlen=size),
                        "force": deque(maxlen=size), "eddy": deque(maxlen=size)}
        for name in self.temps:
            self.buffers[name] = deque(maxlen=size)
        self.window_index = 1
        self.hide_temp_reports = True
        self.console_lines = 0
        self.fan_dragging = set()

        self.content.get_style_context().add_class("bench")
        grid = Gtk.Grid(column_homogeneous=True, column_spacing=6, hexpand=True, vexpand=True)
        grid.attach(self._build_graphs(), 0, 0, 6, 1)
        grid.attach(self._build_right(), 6, 0, 5, 1)
        self.content.add(grid)

        self.prefilled = False
        self.sample_timer = GLib.timeout_add(int(1000 / SAMPLE_HZ), self._sample)

    # ---------- setup helpers ----------

    def _prefill_from_tempstore(self):
        """Seed the graphs with Moonraker's 1 Hz history so they do not start empty."""
        sources = {name: name for name in self.temps}
        flow_sensor = next(
            (n for n in self._printer.get_temp_sensors()
             if (self._printer.get_config_section(n) or {}).get("sensor_type") == "filament_flow"),
            None,
        )
        if flow_sensor:
            sources["flow"] = flow_sensor
        seconds = max(WINDOWS)
        for key, device in sources.items():
            history = self._printer.get_temp_store(device, "temperatures", seconds) or []
            # The store is zero-padded for the time before Klipper started
            start = 0
            while start < len(history) and history[start] == 0:
                start += 1
            values = [None] * start + list(history[start:])
            buffer = self.buffers[key]
            buffer.clear()
            for value in values:
                buffer.extend([value] * SAMPLE_HZ)

    def _install_css(self):
        if Panel.css_provider is not None:
            Gtk.StyleContext.remove_provider_for_screen(Gdk.Screen.get_default(), Panel.css_provider)
        provider = Panel.css_provider = Gtk.CssProvider()
        knob = round(self.base_px * 1.4)
        provider.load_from_data(
            CSS.format(base=self.base_px, small=self.small_px, tiny=self.tiny_px, knob=knob,
                       knobm=round(knob / 2 - 3)).encode()
        )
        # Above the theme (user priority) so the compact sizes win
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_USER + 1
        )

    def _first_section(self, prefix):
        sections = self._printer.get_config_section_list(prefix)
        return sections[0] if sections else None

    def _temperature_objects(self):
        result = []
        for name in self._printer.get_tools() + self._printer.get_heaters():
            if not name.startswith("extruder_stepper"):
                result.append(name)
        real_types = TEMPERATURE_TYPES | self._custom_temperature_types()
        for name in self._printer.get_temp_sensors():
            cfg = self._printer.get_config_section(name) or {}
            # Only real temperature sensors; readouts such as flow or force that
            # Klipper exposes as temperature_sensor are left out
            if cfg.get("sensor_type", "").strip().lower() not in real_types:
                continue
            if name.split(" ", 1)[-1].startswith("_"):
                continue
            result.append(name)
        return result

    def _custom_temperature_types(self):
        """Sensor types defined in printer.cfg with [thermistor x] or [adc_temperature x]."""
        types = set()
        for prefix in ("thermistor ", "adc_temperature "):
            for section in self._printer.get_config_section_list(prefix):
                types.add(section.split(" ", 1)[1].strip().lower())
        return types

    @staticmethod
    def _short(name):
        return name.split(" ", 1)[-1]

    def _label(self, text="", xalign=0.0, css=None, chars=None):
        label = Gtk.Label(label=text, xalign=xalign)
        # Ellipsize every label: its minimum width then no longer depends on
        # the text, so the layout always fits the screen.
        label.set_ellipsize(Pango.EllipsizeMode.END)
        if chars:
            label.set_width_chars(chars)
            label.set_max_width_chars(chars)
        if css:
            label.get_style_context().add_class(css)
        return label

    def _button(self, text, callback, *args, css=None):
        button = Gtk.Button(hexpand=True, vexpand=True, can_focus=False)
        label = Gtk.Label(label=text, justify=Gtk.Justification.CENTER)
        label.set_line_wrap(True)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        button.add(label)
        button.get_style_context().add_class("bench_btn")
        if css:
            button.get_style_context().add_class(css)
        button.connect("clicked", callback, *args)
        button.connect("clicked", self._screen.screensaver.reset_timeout)
        return button

    # ---------- left column: graphs ----------

    def _graph_box(self, title, color, graph, key):
        header = Gtk.Box(spacing=6)
        name = self._label(css="bench_mono")
        name.set_markup(f'<span color="{color}">{title}</span>')
        value = self._label(xalign=1.0, css="bench_mono")
        header.pack_start(name, False, False, 6)
        header.pack_end(value, False, False, 6)
        self.labels[f"title_{key}"] = name
        self.labels[f"cur_{key}"] = value

        events = Gtk.EventBox()
        events.add(graph)
        events.connect("button-release-event", self._cycle_window)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        box.get_style_context().add_class("bench_box")
        box.pack_start(header, False, False, 1)
        box.pack_start(events, True, True, 0)
        return box

    def _build_graphs(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, homogeneous=True, spacing=4)
        self.graphs = []

        flow = BenchGraph(SAMPLE_HZ, self.small_px, zero_based=True, min_span=5)
        flow.add_series(self.buffers["flow_set"], FLOW_SET_COLOR, dashed=True)
        flow.add_series(self.buffers["flow"], FLOW_COLOR)
        if not self.encoder:
            flow.message = _("No filament_encoder configured")
        box.add(self._graph_box(
            f'Flow [mm³/s]  real  <span color="{FLOW_SET_COLOR}">- - set</span>',
            FLOW_COLOR, flow, "flow"))
        self.graphs.append(flow)

        temp = BenchGraph(SAMPLE_HZ, self.small_px, zero_based=False, min_span=2)
        self.temp_colors = {}
        for i, name in enumerate(self.temps):
            color = TEMP_COLORS[i % len(TEMP_COLORS)]
            temp.add_series(self.buffers[name], color)
            self.temp_colors[name] = color
        # The title carries every sensor with its current value (see _refresh)
        box.add(self._graph_box("Temperature [°C]", "#C9CCD1", temp, "temp"))
        self.graphs.append(temp)

        if self.show_force:
            force = BenchGraph(SAMPLE_HZ, self.small_px, zero_based=True, min_span=100)
            force.add_series(self.buffers["force"], FORCE_COLOR)
            self.force_graph = force
            if not self.load_cell:
                force.message = _("No load cell configured")
            title = "Nozzle force [g]"
            if self.eddy is not None:
                force.set_right_axis(EDDY_COLOR, zero_based=False, min_span=EDDY_MIN_SPAN_KHZ)
                force.add_series(self.buffers["eddy"], EDDY_COLOR, right=True)
                title += f'  <span color="{EDDY_COLOR}">eddy [kHz]</span>'
            box.add(self._graph_box(title, FORCE_COLOR, force, "force"))
            self.graphs.append(force)

        for graph in self.graphs:
            graph.set_window(WINDOWS[self.window_index])
        return box

    def _cycle_window(self, *args):
        self.window_index = (self.window_index + 1) % len(WINDOWS)
        for graph in self.graphs:
            graph.set_window(WINDOWS[self.window_index])

    # ---------- right column ----------

    def _build_right(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        fixed = [self._build_favorites(), self._build_monitor_buttons(),
                 self._build_nozzle_buttons()]
        fixed += [self._build_fan(fan) for fan in self.fans]
        box.pack_start(self._build_data(), False, False, 0)
        box.pack_start(self._build_print(), False, False, 0)
        box.pack_start(self._build_console(), True, True, 0)
        for widget in fixed:
            # Buttons expand inside their row; the row itself must not take
            # height from the console.
            widget.set_vexpand(False)
            box.pack_start(widget, False, False, 0)
        return box

    def _build_data(self):
        enc = [("abs", "abs"), ("rel", "rel"), ("edges", "edges"), ("mpe", "mm/edge"),
               ("speed", "speed"), ("flowv", "flow")]
        mon = [("label", "label"), ("elapsed", "elapsed"), ("last", "last"), ("dur", "dur"),
               ("len", "len"), ("qavg", "Q avg/max")]
        sen = [(f"t_{n}", self._short(n)) for n in self.temps[:3]]
        sen += [("force", "force"), ("fmax", "F max"), ("runout", "runout"), ("ratio", "ratio")]
        columns = [("Encoder", "h_enc", enc), ("Monitor", "h_mon", mon), ("Sensors", "h_sen", sen)]

        grid = Gtk.Grid(column_spacing=4, row_spacing=0, column_homogeneous=False)
        grid.get_style_context().add_class("bench_data")
        for col, (head, key, rows) in enumerate(columns):
            title = self._label()
            title.set_markup(f"<b>{head}</b>")
            grid.attach(title, col * 2, 0, 1, 1)
            self.labels[key] = self._label(xalign=1.0, chars=8)
            self.labels[key].set_width_chars(4)
            grid.attach(self.labels[key], col * 2 + 1, 0, 1, 1)
            for row, (vkey, name) in enumerate(rows, start=1):
                label = self._label()
                label.set_markup(f'<span color="#8a929c">{name}</span>')
                grid.attach(label, col * 2, row, 1, 1)
                value = self._label(xalign=1.0, chars=10)
                value.set_width_chars(6)
                value.set_hexpand(True)
                self.labels[f"d_{vkey}"] = value
                grid.attach(value, col * 2 + 1, row, 1, 1)
        return grid

    def _build_print(self):
        """Print status row, shown only while a print is running or paused."""
        info = self._label(css="bench_mono")
        info.set_hexpand(True)
        self.labels["print_info"] = info
        pause = self._button(_("Pause"), self._pause_resume)
        cancel = self._button(_("Cancel"), self._cancel_print, css="bench_stop")
        self.labels["print_pause"] = pause.get_child()
        self.labels["print_cancel"] = cancel.get_child()
        self.cancel_armed = None

        box = Gtk.Box(spacing=4)
        box.get_style_context().add_class("bench_print")
        box.set_size_request(-1, round(self.base_px * 2.4))
        box.pack_start(info, True, True, 4)
        for button in (pause, cancel):
            button.set_hexpand(False)
            button.set_vexpand(False)
            button.set_size_request(round(self.base_px * 5), -1)
            box.pack_start(button, False, False, 0)
        # Hidden until a print starts; content.show_all() must not show it.
        # Show the children now, the row itself is toggled with show()/hide().
        for child in box.get_children():
            child.show_all()
        box.set_no_show_all(True)
        self.print_box = box
        return box

    def _build_console(self):
        entry = Gtk.Entry(hexpand=True, placeholder_text=_("Send code…"))
        entry.set_width_chars(4)
        entry.connect("button-press-event", self._screen.show_keyboard)
        entry.connect("touch-event", self._screen.show_keyboard)
        entry.connect("activate", self._send_command)
        self.labels["entry"] = entry

        bar = Gtk.Box(spacing=3, vexpand=False)
        bar.pack_start(entry, True, True, 0)
        for text, callback in ((_("Send"), self._send_command), (_("Clear"), self._clear_console),
                               (_("Filter"), self._toggle_filter)):
            button = self._button(text, callback)
            button.set_hexpand(False)
            button.set_vexpand(False)
            bar.pack_start(button, False, False, 0)

        buffer = Gtk.TextBuffer()
        view = Gtk.TextView(buffer=buffer, editable=False, cursor_visible=False,
                            wrap_mode=Gtk.WrapMode.NONE, left_margin=6)
        view.connect("button-press-event", self._screen.remove_keyboard)
        scroll = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        scroll.set_policy(Gtk.PolicyType.EXTERNAL, Gtk.PolicyType.AUTOMATIC)
        scroll.add(view)
        self.labels["console"] = buffer

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.get_style_context().add_class("bench_console")
        box.pack_start(bar, False, False, 2)
        box.pack_start(scroll, True, True, 0)
        return box

    def _build_favorites(self):
        grid = Gtk.Grid(column_homogeneous=True, row_homogeneous=True, column_spacing=4,
                        row_spacing=4)
        grid.set_size_request(-1, round(self.base_px * 6.4))
        self.fav_buttons = []
        for i in range(FAV_COUNT):
            button = self._button("", self._run_favorite, i + 1)
            self.fav_buttons.append(button)
            grid.attach(button, i % 4, i // 4, 1, 1)
        menu = self._button(_("Menu"), self._open_menu)
        grid.attach(menu, 3, 1, 1, 1)
        self._update_favorite_labels()
        return grid

    def _build_nozzle_buttons(self):
        box = Gtk.Box(spacing=4, homogeneous=True)
        box.set_size_request(-1, round(self.base_px * 2.9))
        self.nozzle_buttons = {
            "extrude": self._button("", self._move_clicked, 1),
            "retract": self._button("", self._move_clicked, -1),
            "heat": self._button("", self._heat_clicked),
        }
        for button in self.nozzle_buttons.values():
            box.add(button)
        return box

    def _build_monitor_buttons(self):
        box = Gtk.Box(spacing=4, homogeneous=True)
        box.set_size_request(-1, round(self.base_px * 2.9))
        box.add(self._button(_("ENC reset"), self._encoder_cmd, "ENCODER_RESET"))
        box.add(self._button(_("Monitor start"), self._encoder_cmd,
                             "FLOW_MONITOR_START LABEL=manual LOG=1", css="bench_go"))
        box.add(self._button(_("Monitor stop"), self._encoder_cmd, "FLOW_MONITOR_STOP",
                             css="bench_stop"))
        return box

    def _build_fan(self, fan):
        name = self._label(self._short(fan), css="bench_mono")
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 5)
        scale.set_draw_value(False)
        scale.set_hexpand(True)
        scale.connect("button-press-event", lambda *a: self.fan_dragging.add(fan))
        scale.connect("button-release-event", self._fan_released, fan)
        value = self._label("—", xalign=1.0, css="bench_mono", chars=4)
        self.devices[fan] = {"scale": scale, "value": value}

        box = Gtk.Box(spacing=4)
        box.get_style_context().add_class("bench_fan")
        box.set_size_request(-1, round(self.base_px * 2.2))
        box.pack_start(name, False, False, 0)
        box.pack_start(scale, True, True, 0)
        box.pack_start(value, False, False, 0)
        for speed in (0, 100):
            button = self._button(str(speed), self._set_fan, fan, speed)
            button.set_hexpand(False)
            button.set_size_request(round(self.base_px * 3), -1)
            box.pack_start(button, False, False, 0)
        return box

    # ---------- actions ----------

    def _gcode(self, script):
        self._add_console_line("command", time.time(), script)
        self._screen._ws.api.gcode_script(script)

    def _encoder_cmd(self, widget, command):
        if not self.encoder_name:
            self._add_console_line("error", time.time(), "!! No filament_encoder configured")
            return
        self._gcode(f"{command} ENCODER={self.encoder_name}")

    def _favorite_macro(self, number):
        wanted = f"gcode_macro fav_{number}"
        for key in self._printer.get_config_section_list("gcode_macro "):
            if key.lower() == wanted:
                return key
        return None

    def _update_favorite_labels(self):
        for number, button in enumerate(self.fav_buttons, start=1):
            key = self._favorite_macro(number)
            label = button.get_child()
            ctx = button.get_style_context()
            if key:
                cfg = self._printer.get_config_section(key) or {}
                desc = GLib.markup_escape_text(cfg.get("description", f"FAV_{number}"))
                label.set_markup(f'<b>F{number}</b>\n<span size="small">{desc}</span>')
                ctx.remove_class("bench_unset")
            else:
                label.set_markup(f'<b>F{number}</b>\n<span size="small">{_("not set")}</span>')
                ctx.add_class("bench_unset")

    def _run_favorite(self, widget, number):
        key = self._favorite_macro(number)
        if key is None:
            self._add_console_line(
                "info", time.time(),
                f"FAV_{number} is not defined. Add [gcode_macro FAV_{number}] with a "
                "description in the Klipper config (Mainsail).",
            )
            return
        self._gcode(key.split(" ", 1)[1].upper())

    def _pause_resume(self, widget):
        state = self._printer.get_stat("print_stats", "state")
        self._gcode("RESUME" if state == "paused" else "PAUSE")

    def _cancel_print(self, widget):
        # Two taps within 3 s: the first one only arms the button
        if self.cancel_armed is None:
            self.labels["print_cancel"].set_text(_("Sure?"))
            self.cancel_armed = GLib.timeout_add_seconds(3, self._disarm_cancel)
            return
        GLib.source_remove(self.cancel_armed)
        self._disarm_cancel()
        self._gcode("CANCEL_PRINT")

    def _disarm_cancel(self):
        self.cancel_armed = None
        self.labels["print_cancel"].set_text(_("Cancel"))
        return False

    def _refresh_print(self):
        stats = self._printer.data.get("print_stats", {})
        state = stats.get("state", "standby")
        if state not in ("printing", "paused"):
            if self.print_box.get_visible():
                self.print_box.hide()
            return
        progress = self._printer.get_stat("virtual_sdcard", "progress") or 0
        name = (stats.get("filename") or "").rsplit("/", 1)[-1]
        elapsed = int(stats.get("print_duration") or 0)
        eta = ""
        if progress > 0.01:
            left = int(elapsed / progress - elapsed)
            eta = f" · rest {left // 3600}:{left % 3600 // 60:02d} h"
        mark = '<span color="#FAC775">❚❚</span>' if state == "paused" else             '<span color="#5DCAA5">▶</span>'
        self.labels["print_info"].set_markup(
            f"{mark} <b>{progress * 100:.0f}%</b>  {elapsed // 3600}:{elapsed % 3600 // 60:02d} h"
            f"{eta}  {GLib.markup_escape_text(name)}"
        )
        self.labels["print_pause"].set_text(_("Resume") if state == "paused" else _("Pause"))
        if not self.print_box.get_visible():
            self.print_box.show()

    # ---------- nozzle: extrude, retract, heat ----------

    def _nozzle_state(self):
        printing = self._printer.get_stat("print_stats", "state") in ("printing", "paused")
        temp = self._stat(self.extruder, "temperature") or 0.0
        target = self._stat(self.extruder, "target") or 0.0
        return printing, temp, target

    def _move_clicked(self, widget, direction):
        if self.move is not None:
            self._stop_move("Stopped")
            return
        printing, temp, _target = self._nozzle_state()
        if not self.extruder:
            self._add_console_line("error", time.time(), "!! No extruder configured")
        elif printing:
            self._add_console_line("info", time.time(), "Not while a print/test is running")
        elif temp < self.min_extrude_temp:
            self._add_console_line(
                "info", time.time(),
                f"Nozzle too cold: {temp:.0f} °C, needs {self.min_extrude_temp:.0f} °C")
        else:
            length, speed = self.nozzle["bench_extrude_mm"], self.nozzle["bench_extrude_speed"]
            word = "Extrude" if direction > 0 else "Retract"
            self._add_console_line(
                "command", time.time(), f"{word} {length:g} mm at {speed:g} mm/s")
            self.move = {"dir": direction, "done": 0.0, "total": length}
            self._move_step()
            self.move_timer = GLib.timeout_add(int(MOVE_STEP_MM / speed * 1000), self._move_step)
        self._refresh_nozzle()

    def _move_step(self):
        move = self.move
        if move is None:
            return False
        printing, temp, _target = self._nozzle_state()
        if printing or temp < self.min_extrude_temp:
            self._stop_move("Stopped (print started or nozzle too cold)")
            return False
        if move["done"] >= move["total"] - 1e-6:
            # The last step was sent one interval ago and has finished by now
            self._add_console_line("info", time.time(), f"Done: {move['done']:g} mm")
            self.move = None
            self.move_timer = None
            self._refresh_nozzle()
            return False
        step = min(MOVE_STEP_MM, move["total"] - move["done"])
        feed = self.nozzle["bench_extrude_speed"] * 60
        # Relative E only for this move; the gcode state of running tests stays as it was
        self._screen._ws.api.gcode_script(
            "SAVE_GCODE_STATE NAME=bench_move\nM83\n"
            f"G1 E{move['dir'] * step:.3f} F{feed:.0f}\n"
            "RESTORE_GCODE_STATE NAME=bench_move"
        )
        move["done"] += step
        self._refresh_nozzle()
        return True

    def _stop_move(self, reason):
        if self.move_timer is not None:
            GLib.source_remove(self.move_timer)
            self.move_timer = None
        if self.move is not None:
            self._add_console_line(
                "info", time.time(),
                f"{reason} after {self.move['done']:g} mm "
                f"(the current {MOVE_STEP_MM:g} mm step finishes)")
        self.move = None
        self._refresh_nozzle()

    def _heat_clicked(self, widget):
        printing, _temp, target = self._nozzle_state()
        if not self.extruder:
            self._add_console_line("error", time.time(), "!! No extruder configured")
            return
        if printing:
            self._add_console_line("info", time.time(), "Not while a print/test is running")
            return
        if target > 0:
            self._set_nozzle_target(0)
        else:
            self._set_nozzle_target(self.nozzle["bench_heat_temp"])
            self.heat_since = time.time()
        self._refresh_nozzle()

    def _set_nozzle_target(self, target):
        if target <= 0:
            self.heat_since = None
        self._gcode(f"SET_HEATER_TEMPERATURE HEATER={self.extruder} TARGET={target:.0f}")

    def _heat_left(self):
        return self.nozzle["bench_heat_timeout"] * 60 - (time.time() - self.heat_since)

    def _refresh_nozzle(self):
        if not hasattr(self, "nozzle_buttons"):
            return
        printing, temp, target = self._nozzle_state()
        length = self.nozzle["bench_extrude_mm"]
        if target <= 0:
            self.heat_since = None
        elif self.heat_since and not printing and self._heat_left() <= 0:
            self._add_console_line("info", time.time(), "Heat button: timeout, nozzle off")
            self._set_nozzle_target(0)
            target = 0

        for key, direction, word in (("extrude", 1, "Extrude"), ("retract", -1, "Retract")):
            button = self.nozzle_buttons[key]
            ctx = button.get_style_context()
            active = self.move is not None and self.move["dir"] == direction
            if active:
                title, sub = "Stop", f"{self.move['done']:g} / {length:g} mm"
            else:
                title = f"{word} {length:g} mm"
                if printing:
                    sub = "printing"
                elif temp < self.min_extrude_temp:
                    sub = f"needs ≥{self.min_extrude_temp:.0f} °C"
                else:
                    sub = f"{self.nozzle['bench_extrude_speed']:g} mm/s"
            usable = active or (self.move is None and not printing
                                and temp >= self.min_extrude_temp)
            (ctx.add_class if active else ctx.remove_class)("bench_stop")
            (ctx.remove_class if usable else ctx.add_class)("bench_unset")
            button.get_child().set_markup(f'<b>{title}</b>\n<span size="small">{sub}</span>')

        button = self.nozzle_buttons["heat"]
        ctx = button.get_style_context()
        if target > 0:
            title, sub = "Heater off", f"{temp:.0f} → {target:.0f} °C"
            if self.heat_since and not printing:
                sub += f" · {max(0.0, self._heat_left()) / 60:.0f} min"
            ctx.add_class("bench_stop")
            ctx.remove_class("bench_heat")
        else:
            title = f"Heat {self.nozzle['bench_heat_temp']:.0f} °C"
            sub = f"nozzle {temp:.0f} °C"
            ctx.add_class("bench_heat")
            ctx.remove_class("bench_stop")
        (ctx.add_class if printing else ctx.remove_class)("bench_unset")
        if printing:
            sub = "printing"
        button.get_child().set_markup(f'<b>{title}</b>\n<span size="small">{sub}</span>')

    def _open_menu(self, widget):
        self._screen.show_panel("main_menu", _("Menu"), items=self._config.get_menu_items("__main"))

    def _fan_released(self, widget, event, fan):
        self.fan_dragging.discard(fan)
        self._set_fan(widget, fan, widget.get_value())

    def _set_fan(self, widget, fan, speed):
        if fan == "fan":
            self._gcode(f"M106 S{float(speed) * 2.55:.0f}")
        else:
            self._gcode(f"SET_FAN_SPEED FAN={self._short(fan)} SPEED={float(speed) / 100:.2f}")
        self.devices[fan]["scale"].set_value(float(speed))

    # ---------- console ----------

    def _send_command(self, *args):
        entry = self.labels["entry"]
        cmd = entry.get_text().strip()
        entry.set_text("")
        self._screen.remove_keyboard()
        if cmd:
            self._gcode(cmd)

    def _clear_console(self, *args):
        self.labels["console"].set_text("")
        self.console_lines = 0

    def _toggle_filter(self, *args):
        self.hide_temp_reports = not self.hide_temp_reports
        state = _("hidden") if self.hide_temp_reports else _("shown")
        self._add_console_line("info", time.time(), f"Temperature reports {state}")

    def _add_console_line(self, msgtype, msgtime, message):
        if msgtype != "command" and TEMP_REPORT.match(message) and self.hide_temp_reports:
            return
        if msgtype == "command":
            color = CONSOLE_COLORS["command"]
        elif msgtype == "info":
            color = CONSOLE_COLORS["info"]
        elif message.startswith("!!"):
            color = CONSOLE_COLORS["error"]
        else:
            color = CONSOLE_COLORS["response"]
        stamp = datetime.fromtimestamp(msgtime).strftime("%H:%M:%S")
        text = GLib.markup_escape_text(message.replace("\n", " "))
        buffer = self.labels["console"]
        prefix = "" if self.console_lines == 0 else "\n"
        # Newest line on top, like the Mainsail console
        buffer.insert_markup(
            buffer.get_start_iter(),
            f'<span color="{CONSOLE_COLORS["time"]}">{stamp}</span>  '
            f'<span color="{color}">{text}</span>{prefix}',
            -1,
        )
        self.console_lines += 1
        if self.console_lines > CONSOLE_LINES:
            end = buffer.get_end_iter()
            start = buffer.get_iter_at_line(CONSOLE_LINES)
            start.backward_char()
            buffer.delete(start, end)
            self.console_lines = CONSOLE_LINES

    def _gcode_store(self, result, method, params):
        if method != "server.gcode_store" or "result" not in result:
            return
        self._clear_console()
        for item in result["result"].get("gcode_store", []):
            self._add_console_line(item["type"], item["time"], item["message"])

    # ---------- data ----------

    def _stat(self, obj, field):
        if not obj:
            return None
        return self._printer.get_stat(obj, field) if obj in self._printer.data else None

    def _force(self):
        if not self.load_cell:
            return None
        data = self._printer.data.get(self.load_cell, {})
        for field in ("force_g", "force"):
            if isinstance(data.get(field), (int, float)):
                return data[field]
        return None

    def _flow_set(self):
        """Expected flow: live extruder velocity times the filament cross-section."""
        velocity = self._stat("motion_report", "live_extruder_velocity")
        if velocity is None or not self.extruder:
            return None
        return velocity * self.filament_area

    def _eddy_khz(self):
        if self.eddy is None or self.eddy_failed:
            return None
        try:
            hz = self.eddy.poll()
        except Exception as e:
            # Keep the sample timer alive; the eddy line just stays empty
            logging.exception(f"bench eddy failed, switched off until restart: {e}")
            self.eddy_failed = True
            return None
        return None if hz is None else hz / 1000

    def _sample(self):
        # Moonraker's temperature store arrives shortly after the panel is built
        if not self.prefilled and self._printer.tempstore:
            self.prefilled = True
            self._prefill_from_tempstore()
        self.buffers["flow"].append(self._stat(self.encoder, "flow"))
        self.buffers["flow_set"].append(self._flow_set())
        self.buffers["force"].append(self._force())
        self.buffers["eddy"].append(self._eddy_khz())
        for name in self.temps:
            self.buffers[name].append(self._stat(name, "temperature"))
        return True

    def _refresh(self):
        enc = self._printer.data.get(self.encoder, {}) if self.encoder else {}
        mon = enc.get("monitor") or {}
        last = enc.get("last_result") or {}
        set_ = lambda key, text: self.labels[key].set_text(text)

        set_("d_abs", fmt(enc.get("absolute"), 2, "mm"))
        set_("d_rel", fmt(enc.get("relative"), 2, "mm"))
        set_("d_edges", fmt(enc.get("relative_edges"), 0))
        set_("d_mpe", fmt(enc.get("mm_per_edge"), 3))
        set_("d_speed", fmt(enc.get("speed"), 2, "mm/s"))
        set_("d_flowv", fmt(enc.get("flow"), 2, "mm³/s"))

        running = bool(mon.get("active"))
        self.labels["h_mon"].set_markup(
            '<span color="#5DCAA5"><b>RUN</b></span>' if running else "idle"
        )
        set_("d_label", (mon.get("label") or "—") if running else "—")
        set_("d_elapsed", fmt(mon.get("elapsed"), 1, "s") if running else "—")
        set_("d_last", last.get("label") or "—")
        set_("d_dur", fmt(last.get("duration"), 1, "s"))
        set_("d_len", fmt(last.get("length"), 1, "mm"))
        qavg, qmax = last.get("flow_avg"), last.get("flow_max")
        set_("d_qavg", "—" if qavg is None else f"{fmt(qavg, 1)}/{fmt(qmax, 0)}")

        for name in self.temps[:3]:
            set_(f"d_t_{name}", fmt(self._stat(name, "temperature"), 2, "°C"))
        force = self._force()
        set_("d_force", fmt(force, 0, "g"))
        if self.load_cell and getattr(self, "force_graph", None) is not None:
            calibrated = self._printer.data.get(self.load_cell, {}).get("is_calibrated", True)
            self.force_graph.message = None if calibrated else _("Load cell not calibrated")
        values = [v for v in self.buffers["force"] if v is not None]
        set_("d_fmax", fmt(max(values), 0, "g") if values else "—")
        if self.runout:
            data = self._printer.data.get(self.runout, {})
            if not data.get("enabled", True):
                self.labels["d_runout"].set_text("off")
            elif data.get("filament_detected"):
                self.labels["d_runout"].set_markup('<span color="#5DCAA5">ok</span>')
            else:
                self.labels["d_runout"].set_markup('<span color="#F09595">EMPTY</span>')
        else:
            set_("d_runout", "—")
        ratio = mon.get("ratio") if running else last.get("ratio")
        set_("d_ratio", fmt(ratio, 3))

        self.labels["cur_flow"].set_markup(
            f'<span color="{FLOW_COLOR}">{fmt(enc.get("flow"), 2)}</span>  '
            f'<span color="{FLOW_SET_COLOR}">set {fmt(self._flow_set(), 2)}</span>'
        )
        parts = [
            f'<span color="{color}">{GLib.markup_escape_text(self._short(name))}</span> '
            f'{fmt(self._stat(name, "temperature"), 1)}'
            for name, color in self.temp_colors.items()
        ]
        self.labels["title_temp"].set_markup('<span color="#C9CCD1">°C</span>  ' + "  ".join(parts))
        if "cur_force" in self.labels:
            text = f'<span color="{FORCE_COLOR}">{fmt(force, 0)}</span>'
            if self.eddy is not None:
                khz = self.buffers["eddy"][-1] if self.buffers["eddy"] else None
                if khz is None:
                    # Only the testbench recorder reads the PCB, during 'bench.py watch'
                    text += '  <span color="#6B7480">eddy: not recording</span>'
                else:
                    text += f'  <span color="{EDDY_COLOR}">{fmt(khz, 3)}</span>'
            self.labels["cur_force"].set_markup(text)

        self._refresh_print()
        self._refresh_nozzle()

        for fan, widgets in self.devices.items():
            speed = self._printer.get_fan_speed(fan)
            widgets["value"].set_text(f"{speed * 100:.0f}%")
            if fan not in self.fan_dragging:
                widgets["scale"].set_value(speed * 100)

    def _tick(self):
        try:
            self._refresh()
        except Exception as e:
            logging.exception(f"bench refresh failed: {e}")
        for graph in self.graphs:
            graph.queue_draw()
        return True

    # ---------- panel lifecycle ----------

    def activate(self):
        self._update_favorite_labels()
        self._screen._ws.send_method("server.gcode_store", {"count": 60}, self._gcode_store)
        self._tick()
        if self.draw_timer is None:
            self.draw_timer = GLib.timeout_add(500, self._tick)

    def deactivate(self):
        if self.draw_timer is not None:
            GLib.source_remove(self.draw_timer)
            self.draw_timer = None

    def process_update(self, action, data):
        if action == "notify_gcode_response":
            self._add_console_line("response", time.time(), data)
