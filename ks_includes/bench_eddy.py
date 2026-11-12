"""Live values of the eddy-current force sensor for the test bench panel.

The eddy-current PCB is not a Klipper object. Normally the eddy service of
the private testbench repo (tools/eddy_service.py) owns it all the time and
writes the mean frequency about every 0.25 s into a small JSON file in RAM
(option bench_eddy_live, default /dev/shm/eddy_live.json); this module reads
that file.

Without the service, 'bench.py watch' runs tools/eddy_record.py during a
run, which writes logs/eddy[_N].csv, flushed about every 0.5 s. Then this
module follows the newest of those files (option bench_eddy_glob) and turns
the raw LDC161x values into Hz with the reference frequency that the
recorder logs when the stream starts.

It never opens the PCB itself, so it cannot disturb a recording, and it
needs no protocol code.
"""

import glob
import json
import logging
import os
import time

RAW_SCALE = 2**28  # LDC161x: f_sensor = raw * f_ref / 2^28
SCAN_S = 5.0  # how often to look for a recording while none is followed
STALE_S = 3.0  # a file without new rows for this long has ended
TAIL_BYTES = 4096  # where to start in a file that is opened or fell behind
MAX_BACKLOG = 256 * 1024  # a longer backlog is skipped, the graph shows live values
NO_SERVICE = object()
# Service states as the panel shows them
STATE_TEXT = {
    "no_device": "no PCB",
    "connecting": "connecting",
    "error": "PCB error",
    "stopped": "service stopped",
    "streaming": "no samples",
}


class EddyTail:
    def __init__(self, patterns, live_path=None):
        self.patterns = [os.path.expanduser(p.strip()) for p in patterns.split(",") if p.strip()]
        self.live_path = os.path.expanduser(live_path.strip()) if live_path and live_path.strip() else None
        self.status = "not recording"  # why there is no value; None while values come
        self.file = self.path = self.fref = None
        self.raw_col, self.ncols = 2, None
        self.rest = b""
        self.hz = None
        self.last_rows = 0.0
        self.next_scan = 0.0
        self.warned = set()

    @property
    def live(self):
        return (
            self.file is not None
            and self.fref is not None
            and time.monotonic() - self.last_rows < STALE_S
        )

    def poll(self):
        """-> frequency in Hz, or None; then self.status says why."""
        if self.live_path:
            hz = self._poll_live()
            if hz is not NO_SERVICE:
                return hz
        hz = self._poll_csv()
        if hz is not None:
            self.status = None
        else:
            self.status = "service off" if self.live_path else "not recording"
        return hz

    def _poll_live(self):
        try:
            with open(self.live_path, encoding="utf-8") as f:
                live = json.load(f)
        except (OSError, ValueError):
            return NO_SERVICE
        if not isinstance(live, dict) or time.time() - (live.get("unix_time") or 0) > STALE_S:
            return NO_SERVICE  # the service is not running
        hz = live.get("freq_hz")
        if live.get("state") == "streaming" and isinstance(hz, (int, float)):
            self.status = None
            return float(hz)
        self.status = STATE_TEXT.get(live.get("state"), str(live.get("state")))
        return None

    def _poll_csv(self):
        """Read the new rows -> frequency in Hz (mean of the newest rows), None without a live recording."""
        now = time.monotonic()
        if self.file is not None and now - self.last_rows > STALE_S:
            self._close()
        if self.file is None:
            if now < self.next_scan:
                return None
            self.next_scan = now + SCAN_S
            self._open_newest()
            if self.file is None:
                return None
        if self.fref is None:
            self.fref = self._read_fref()
        try:
            self._read_rows(now)
        except (OSError, ValueError) as e:
            logging.warning(f"bench eddy: reading {self.path} failed: {e}")
            self._close()
            return None
        return self.hz if self.live else None

    def _read_rows(self, now):
        size = os.fstat(self.file.fileno()).st_size
        pos = self.file.tell()
        if size < pos:  # truncated or replaced
            raise ValueError("file got shorter")
        if size - pos > MAX_BACKLOG:
            self._seek_tail(size)
        chunk = self.file.read()
        if not chunk:
            return
        lines = (self.rest + chunk).split(b"\n")
        self.rest = lines.pop()
        raws = []
        for line in lines:
            fields = line.split(b",")
            if self.ncols is not None and len(fields) != self.ncols:
                continue  # a broken row
            try:
                raws.append(int(fields[self.raw_col]))
            except (IndexError, ValueError):
                continue  # header or a broken row
        if raws:
            self.last_rows = now
            if self.fref is not None:
                self.hz = sum(raws) / len(raws) * self.fref / RAW_SCALE

    def _open_newest(self):
        now, best = time.time(), None
        for pattern in self.patterns:
            for path in glob.glob(pattern):
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    continue
                if now - mtime < STALE_S and (best is None or mtime > best[0]):
                    best = (mtime, path)
        if best is None:
            return
        path = best[1]
        try:
            f = open(path, "rb")
            header = f.readline().strip().split(b",")
            if b"raw" in header:
                self.raw_col, self.ncols = header.index(b"raw"), len(header)
            else:
                self.raw_col, self.ncols = 2, None
            self.file, self.path, self.fref, self.hz = f, path, None, None
            self._seek_tail(os.fstat(f.fileno()).st_size)
        except OSError as e:
            logging.warning(f"bench eddy: cannot open {path}: {e}")
            return
        self.last_rows = time.monotonic()
        logging.info(f"bench eddy: following {path}")

    def _seek_tail(self, size):
        pos = self.file.tell()
        start = max(pos, size - TAIL_BYTES)
        if start > pos:
            self.file.seek(start - 1)
            if self.file.read(1) != b"\n":
                self.file.readline()  # the rest of a row that started before
        self.rest = b""

    def _read_fref(self):
        """f_ref from the 'streaming: {...}' line of the recorder log next to the CSV."""
        log = self.path[: -len(".csv")] + "_record.log"
        try:
            with open(log, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.startswith("streaming:"):
                        fref = json.loads(line.split(":", 1)[1]).get("fref_hz")
                        if isinstance(fref, (int, float)) and fref > 0:
                            return float(fref)
        except (OSError, ValueError):
            pass
        if log not in self.warned:
            self.warned.add(log)
            logging.info(f"bench eddy: no f_ref in {log} yet, no values until it is there")
        return None

    def _close(self):
        if self.file is not None:
            try:
                self.file.close()
            except OSError:
                pass
            logging.info(f"bench eddy: {self.path} ended")
        self.file = self.path = self.fref = self.hz = None
        self.rest = b""
        self.next_scan = 0.0
