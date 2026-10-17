#!/usr/bin/env python3
"""
DNS Benchmark GUI — dark-themed dashboard for the GRC-style DNS benchmark.

Wraps dns_benchmark.py's core (raw UDP DNS, zero third-party deps) with:
  * live results table (sortable — click any column heading)
  * rank-colored bar chart of any metric (Score / Cached / Uncached / Dot-Com)
  * per-query progress bar, abortable runs, CSV export
  * system-resolver detection and a hint when public resolvers are blocked

Usage:
  python dns_benchmark_gui.py
  python dns_benchmark_gui.py --samples 10 --auto     # quick headless test run
"""

import argparse
import csv
import ctypes
import queue
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import tkinter.ttk as ttk
from tkinter import filedialog

from dns_benchmark import (BenchmarkAborted, KNOWN_SERVERS, benchmark_server,
                           get_system_resolvers, is_ip)

# ---------------------------------------------------------------------------
# Theme (matches the WiFi Network Monitor)
# ---------------------------------------------------------------------------

BG      = "#0b0f14"
PANEL   = "#11161d"
CARD    = "#151c26"
BORDER  = "#263041"
TEXT    = "#e6edf3"
MUTED   = "#8b949e"
GREEN   = "#4ade80"
RED     = "#f87171"
YELLOW  = "#fbbf24"
BLUE    = "#60a5fa"
CYAN    = "#22d3ee"
PURPLE  = "#c084fc"

STATUS_STYLES = {
    "IDLE":    (MUTED,   "● IDLE"),
    "RUNNING": (YELLOW,  "● RUNNING"),
    "DONE":    (GREEN,   "● DONE"),
    "STOPPED": (YELLOW,  "● STOPPED"),
    "ERROR":   (RED,     "● ERROR"),
}

METRICS = {"score": "Score", "cached": "Cached", "uncached": "Uncached",
           "dotcom": "Dot-Com"}


def blend(c1: str, c2: str, t: float) -> str:
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{int(x + (y - x) * t):02x}" for x, y in zip(a, b))


def rank_color(t: float) -> str:
    """GREEN -> YELLOW -> RED gradient, t in 0..1."""
    if t < 0.5:
        return blend(GREEN, YELLOW, t * 2)
    return blend(YELLOW, RED, (t - 0.5) * 2)


def score_of(r: dict) -> float:
    return (r["cached"] + r["uncached"]) / max(r["reliability"] / 100.0, 0.05)


# ---------------------------------------------------------------------------
# Server list + worker thread
# ---------------------------------------------------------------------------

def build_server_list(include_system: bool):
    servers, labels = [], {}
    for name, ips in KNOWN_SERVERS.items():
        for ip in ips:
            servers.append(ip)
            labels[ip] = name
    if include_system:
        for ip in get_system_resolvers():
            if ip not in servers:
                servers.append(ip)
                labels[ip] = f"{ip} (system)"
    return servers, labels


def run_benchmark(q: queue.Queue, stop: threading.Event,
                  include_system: bool, samples: int, timeout: float, workers: int):
    """Worker thread: pushes ('setup'…), ('progress'…), ('result'…), ('done'…)."""
    try:
        servers, labels = build_server_list(include_system)
        if not servers:
            q.put(("error", "No DNS servers to test."))
            return
        q.put(("setup", len(servers), labels))
        total_units = len(servers) * 3 * samples
        lock = threading.Lock()
        units = {"n": 0}

        def progress(_phase, _done, _total):
            with lock:
                units["n"] += 1
                q.put(("progress", units["n"], total_units))

        def work(server):
            return benchmark_server(server, samples, timeout,
                                    stop=stop, progress=progress)

        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(work, s): s for s in servers}
            for fut in as_completed(futures):
                r = fut.result()  # BenchmarkAborted propagates
                q.put(("result", r))
        q.put(("done", None))
    except BenchmarkAborted:
        q.put(("done", "stopped"))
    except Exception as e:
        q.put(("error", str(e)))


# ---------------------------------------------------------------------------
# Bar chart widget
# ---------------------------------------------------------------------------

class BarChart(tk.Canvas):
    def __init__(self, parent, fonts, **kw):
        super().__init__(parent, bg=CARD, highlightthickness=1,
                         highlightbackground=BORDER, **kw)
        self.fonts = fonts
        self.rows = []       # list of (label, value, is_system)
        self.metric = "score"
        self.bind("<Configure>", lambda e: self.redraw())

    def set_data(self, rows, metric):
        self.rows, self.metric = rows, metric
        self.redraw()

    def redraw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 120 or h < 60 or not self.rows:
            if self.winfo_width() > 120:
                self.create_text(w / 2, h / 2, text="no results yet",
                                 fill=MUTED, font=self.fonts["small"])
            return
        f = self.fonts
        self.create_text(10, 14, anchor="w", fill=MUTED, font=f["small"],
                         text=f"{METRICS[self.metric].upper()} — lower is better")

        inf = float("inf")
        key = (lambda r: r[1]) if self.metric != "reliability" else (lambda r: -r[1])
        rows = sorted(self.rows, key=key)
        finite = [r[1] for r in rows if r[1] != inf]
        maxval = max(finite) if finite else 1.0

        pad_r, pad_t, pad_b = 60, 34, 10
        longest = max((len(r[0]) for r in rows), default=10)
        pad_l = min(230, max(150, int(self.fonts["tiny"].measure("M" * longest)) + 20))
        n = len(rows)
        bar_h = max(10, min(30, (h - pad_t - pad_b) / max(1, n)))
        x0, x1 = pad_l, w - pad_r
        span = max(1e-9, x1 - x0)

        for i, (label, val, _sys) in enumerate(rows):
            y = pad_t + i * (h - pad_t - pad_b) / max(1, n) + (bar_h / 2)
            t = i / max(1, n - 1)
            color = rank_color(t)
            name = label if len(label) <= 24 else label[:23] + "…"
            self.create_text(x0 - 8, y, anchor="e", text=name,
                             fill=MUTED if _sys else TEXT, font=f["tiny"])
            if val == inf:
                self.create_text(x0 + 4, y, anchor="w", text="— unreachable —",
                                 fill=RED, font=f["tiny"])
                continue
            frac = 0.02 if val <= 0 else min(1.0, val / maxval)
            self.create_rectangle(x0, y - bar_h / 2 + 1, x0 + frac * span,
                                  y + bar_h / 2 - 1, fill=color, outline="")
            best = (i == 0)
            self.create_text(x0 + frac * span + 6, y, anchor="w",
                             text=f"{val:.1f}" if val < 10_000 else f"{val:,.0f}",
                             fill=GREEN if best else MUTED,
                             font=f["value" if best else "tiny"])


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

class Dashboard(tk.Tk):
    SORT_KEYS = {"server": lambda r: r["label"],
                 "cached": lambda r: r["cached"],
                 "uncached": lambda r: r["uncached"],
                 "dotcom": lambda r: r["dotcom"],
                 "rel": lambda r: -r["reliability"],
                 "score": lambda r: score_of(r)}

    def __init__(self, opts):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
            scale = ctypes.windll.user32.GetDpiForSystem() / 96.0
        except Exception:
            scale = 1.0
        super().__init__()
        self.title("DNS Benchmark")
        self.configure(bg=BG)
        self.geometry("1280x820")
        self.minsize(1000, 640)

        s = scale
        self.fonts = {
            "title":  tkfont.Font(family="Segoe UI", size=-int(13 * s), weight="bold"),
            "small":  tkfont.Font(family="Segoe UI", size=-int(10 * s)),
            "tiny":   tkfont.Font(family="Segoe UI", size=-int(9 * s)),
            "value":  tkfont.Font(family="Consolas", size=-int(10 * s), weight="bold"),
            "big":    tkfont.Font(family="Segoe UI", size=-int(14 * s), weight="bold"),
            "sub":    tkfont.Font(family="Consolas", size=-int(9 * s)),
        }

        self.opts = opts
        self.q = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self.results = {}          # ip -> result dict
        self.labels = {}           # ip -> display label
        self.n_servers = 0
        self.sort_key = "score"
        self.running = False
        self.t0 = 0.0

        self._build_header()
        self._build_controls()
        self._build_body()
        self._build_footer()
        self._style_tree()

        self.after(100, self._poll)
        self.after(1000, self._tick)
        if opts.auto:
            self.after(300, self._toggle_run)

    # ---- header ----

    def _build_header(self):
        bar = tk.Frame(self, bg=PANEL, pady=10, padx=14)
        bar.grid(row=0, column=0, sticky="ew")
        self.grid_columnconfigure(0, weight=1)
        tk.Label(bar, text="DNS BENCHMARK", bg=PANEL, fg=TEXT,
                 font=self.fonts["title"]).pack(side="left")
        self.status_lbl = tk.Label(bar, text=STATUS_STYLES["IDLE"][1], bg=PANEL,
                                   fg=MUTED, font=self.fonts["title"])
        self.status_lbl.pack(side="left", padx=18)
        self.clock_lbl = tk.Label(bar, text="", bg=PANEL, fg=TEXT,
                                  font=self.fonts["small"])
        self.clock_lbl.pack(side="right")

        tk.Frame(self, bg=BORDER, height=1).grid(row=1, column=0, sticky="ew")

    # ---- controls ----

    def _ctl_label(self, parent, text):
        return tk.Label(parent, text=text, bg=BG, fg=MUTED, font=self.fonts["small"])

    def _build_controls(self):
        row = tk.Frame(self, bg=BG, pady=8, padx=10)
        row.grid(row=2, column=0, sticky="ew")

        self._ctl_label(row, "Samples per phase:").pack(side="left")
        self.samples = tk.Spinbox(row, from_=2, to=200, width=5,
                                  bg=CARD, fg=TEXT, buttonbackground=PANEL,
                                  insertbackground=TEXT, relief="flat",
                                  font=self.fonts["small"])
        self.samples.delete(0, "end")
        self.samples.insert(0, str(self.opts.samples))
        self.samples.pack(side="left", padx=(4, 14), ipady=2)

        self._ctl_label(row, "Timeout (s):").pack(side="left")
        self.timeout = tk.Spinbox(row, from_=0.5, to=10, increment=0.5, width=5,
                                  bg=CARD, fg=TEXT, buttonbackground=PANEL,
                                  insertbackground=TEXT, relief="flat",
                                  font=self.fonts["small"])
        self.timeout.delete(0, "end")
        self.timeout.insert(0, str(self.opts.timeout))
        self.timeout.pack(side="left", padx=(4, 14), ipady=2)

        self.include_system = tk.BooleanVar(value=True)
        tk.Checkbutton(row, text="Include system DNS", variable=self.include_system,
                       bg=BG, fg=TEXT, activebackground=BG, activeforeground=TEXT,
                       selectcolor=CARD, relief="flat", highlightthickness=0,
                       font=self.fonts["small"]).pack(side="left", padx=(0, 14))

        self.run_btn = tk.Button(row, text="▶  Run benchmark", command=self._toggle_run,
                                 bg="#1f3a2a", fg=GREEN, activebackground=BORDER,
                                 activeforeground=TEXT, relief="flat", padx=14,
                                 font=self.fonts["small"])
        self.run_btn.pack(side="left")

        self.export_btn = tk.Button(row, text="Export CSV", command=self._export,
                                    bg=CARD, fg=MUTED, activebackground=BORDER,
                                    activeforeground=TEXT, relief="flat", padx=12,
                                    state="disabled", font=self.fonts["small"])
        self.export_btn.pack(side="right")

    # ---- body: table + chart ----

    def _build_body(self):
        body = tk.Frame(self, bg=BG)
        body.grid(row=3, column=0, sticky="nsew", padx=8, pady=6)
        self.grid_rowconfigure(3, weight=1)
        body.grid_columnconfigure(0, weight=3, uniform="b")
        body.grid_columnconfigure(1, weight=2, uniform="b")
        self.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(0, weight=1)

        table_frame = tk.Frame(body, bg=CARD, highlightthickness=1,
                               highlightbackground=BORDER)
        table_frame.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)

        cols = ("server", "cached", "uncached", "dotcom", "rel", "score")
        self.tree = ttk.Treeview(table_frame, columns=cols, show="headings")
        for col, text, width, anchor in (
                ("server", "Server", 220, "w"), ("cached", "Cached", 90, "e"),
                ("uncached", "Uncached", 95, "e"), ("dotcom", "Dot-Com", 90, "e"),
                ("rel", "Reliability", 95, "e"), ("score", "Score", 70, "e")):
            self.tree.heading(col, text=text,
                              command=lambda c=col: self._set_sort(c))
            self.tree.column(col, width=width, anchor=anchor, stretch=True)
        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        for tag, color in (("top", GREEN), ("bad", RED), ("norm", TEXT)):
            self.tree.tag_configure(tag, foreground=color)

        right = tk.Frame(body, bg=BG)
        right.grid(row=0, column=1, sticky="nsew", padx=4, pady=4)
        right.grid_rowconfigure(0, weight=1)
        right.grid_columnconfigure(0, weight=1)
        self.chart = BarChart(right, self.fonts)
        self.chart.grid(row=0, column=0, sticky="nsew")

        pick = tk.Frame(right, bg=BG)
        pick.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        tk.Label(pick, text="Chart metric:", bg=BG, fg=MUTED,
                 font=self.fonts["tiny"]).pack(side="left")
        self.metric_var = tk.StringVar(value="score")
        for key, label in METRICS.items():
            tk.Radiobutton(pick, text=label, variable=self.metric_var, value=key,
                           command=self._render, bg=BG, fg=TEXT,
                           activebackground=BG, activeforeground=TEXT,
                           selectcolor=CARD, relief="flat", highlightthickness=0,
                           font=self.fonts["tiny"]).pack(side="left", padx=2)

    def _style_tree(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Treeview", background=CARD, fieldbackground=CARD,
                        foreground=TEXT, rowheight=26, borderwidth=0,
                        font=self.fonts["tiny"])
        style.configure("Treeview.Heading", background=PANEL, foreground=MUTED,
                        relief="flat", font=self.fonts["tiny"])
        style.map("Treeview.Heading", background=[("active", PANEL)])
        style.map("Treeview", background=[("selected", "#1c3a5e")],
                  foreground=[("selected", TEXT)])
        style.configure("Vertical.TScrollbar", background=PANEL,
                        troughcolor=BG, bordercolor=BORDER)
        style.configure("dark.Horizontal.TProgressbar", troughcolor=BG,
                        background=GREEN, bordercolor=BG, thickness=10)

    # ---- footer ----

    def _build_footer(self):
        self.grid_rowconfigure(5, weight=0)
        prog = tk.Frame(self, bg=BG)
        prog.grid(row=4, column=0, sticky="ew", padx=10, pady=(0, 2))
        self.progress = ttk.Progressbar(prog, style="dark.Horizontal.TProgressbar",
                                        maximum=1.0, value=0)
        self.progress.pack(side="left", fill="x", expand=True)
        self.prog_lbl = tk.Label(prog, text="idle", bg=BG, fg=MUTED,
                                 font=self.fonts["sub"])
        self.prog_lbl.pack(side="left", padx=(10, 0))

        self.summary_lbl = tk.Label(self, text="", bg=PANEL, fg=TEXT, anchor="w",
                                    font=self.fonts["sub"], padx=10, pady=6)
        self.summary_lbl.grid(row=5, column=0, sticky="ew")
        self.hint_lbl = tk.Label(self, text="", bg=PANEL, fg=YELLOW, anchor="w",
                                 font=self.fonts["sub"], padx=10)
        self.hint_lbl.grid(row=6, column=0, sticky="ew")

    # ---- actions ----

    def _toggle_run(self):
        if self.running:
            self.stop_event.set()
            self.run_btn.config(state="disabled")
            return
        try:
            samples = max(1, int(self.samples.get()))
            timeout = max(0.2, float(self.timeout.get()))
        except ValueError:
            self._set_status("ERROR", "invalid samples/timeout")
            return
        self.results, self.labels, self.n_servers = {}, {}, 0
        self._render()
        self.stop_event = threading.Event()
        self.running = True
        self.t0 = time.time()
        self.run_btn.config(text="■  Stop", bg="#3a1f1f", fg=RED)
        self.export_btn.config(state="disabled", fg=MUTED)
        self.summary_lbl.config(text="")
        self.hint_lbl.config(text="")
        self._set_status("RUNNING")
        self.prog_lbl.config(text="resolving server list…")
        self.worker = threading.Thread(target=run_benchmark, daemon=True,
                                       args=(self.q, self.stop_event,
                                             self.include_system.get(),
                                             samples, timeout, 10))
        self.worker.start()

    def _export(self):
        if not self.results:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", initialfile="dns_benchmark_results.csv",
            filetypes=[("CSV files", "*.csv")])
        if not path:
            return
        rows = sorted(self.results.values(), key=score_of)
        with open(path, "w", newline="", encoding="utf-8") as f:
            wtr = csv.writer(f)
            wtr.writerow(["rank", "server", "ip", "cached_ms", "uncached_ms",
                          "dotcom_ms", "reliability_pct", "score"])
            for i, r in enumerate(rows, 1):
                wtr.writerow([i, self.labels.get(r["server"], r["server"]),
                              r["server"],
                              "" if r["cached"] == float("inf") else f"{r['cached']:.1f}",
                              "" if r["uncached"] == float("inf") else f"{r['uncached']:.1f}",
                              "" if r["dotcom"] == float("inf") else f"{r['dotcom']:.1f}",
                              f"{r['reliability']:.1f}", f"{score_of(r):.1f}"])

    def _set_sort(self, col):
        if col in self.SORT_KEYS:
            self.sort_key = col
            self._render()

    def _set_status(self, key, text=None):
        color, label = STATUS_STYLES[key]
        self.status_lbl.config(text=text or label, fg=color)

    # ---- polling ----

    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "setup":
                    _, n, labels = msg
                    self.n_servers = n
                    self.labels.update(labels)
                elif kind == "progress":
                    _, units, total = msg
                    self.progress.config(maximum=total, value=units)
                    self.prog_lbl.config(text=f"{units} / {total} queries")
                elif kind == "result":
                    r = msg[1]
                    self.results[r["server"]] = r
                    self._render()
                elif kind == "done":
                    self._finish("STOPPED" if msg[1] == "stopped" else "DONE")
                elif kind == "error":
                    self._finish("ERROR", msg[1])
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _finish(self, status, err=None):
        self.running = False
        self.run_btn.config(text="▶  Run benchmark", bg="#1f3a2a", fg=GREEN,
                            state="normal")
        if self.results:
            self.export_btn.config(state="normal", fg=TEXT)
        self._set_status(status, err and f"● ERROR — {err}")
        if status == "DONE":
            self.progress.config(value=self.progress["maximum"])
            self.prog_lbl.config(
                text=f"{len(self.results)} servers in {time.time() - self.t0:.0f}s")
        elif status == "STOPPED":
            self.prog_lbl.config(text="stopped")
        elif status == "ERROR":
            self.prog_lbl.config(text=err or "error")

    def _tick(self):
        self.clock_lbl.config(text=time.strftime("%H:%M:%S"))
        self.after(1000, self._tick)

    # ---- rendering ----

    def _render(self):
        # --- table ---
        self.tree.delete(*self.tree.get_children())
        rows = list(self.results.values())
        rows.sort(key=self.SORT_KEYS[self.sort_key])
        inf = float("inf")
        for i, r in enumerate(rows):
            label = self.labels.get(r["server"], r["server"])
            score = score_of(r)
            vals = (f"{r['cached']:.1f}" if r["cached"] != inf else "---",
                    f"{r['uncached']:.1f}" if r["uncached"] != inf else "---",
                    f"{r['dotcom']:.1f}" if r["dotcom"] != inf else "---",
                    f"{r['reliability']:.1f} %",
                    f"{score:.1f}" if score != inf else "---")
            tag = "bad" if r["reliability"] < 50 else ("top" if i == 0 else "norm")
            self.tree.insert("", "end", values=(label, *vals), tags=(tag,))

        # --- chart ---
        chart_rows = [(self.labels.get(r["server"], r["server"]),
                       score_of(r) if self.metric_var.get() == "score" else r[self.metric_var.get()],
                       "(system)" in self.labels.get(r["server"], ""))
                      for r in rows]
        self.chart.set_data(chart_rows, self.metric_var.get())

        # --- summary ---
        if rows:
            L = lambda r: self.labels.get(r["server"], r["server"])

            def best_by(key):
                fin = [r for r in rows if key(r) != inf]
                return min(fin, key=key) if fin else None

            best = best_by(score_of)
            fc = best_by(lambda r: r["cached"])
            fu = best_by(lambda r: r["uncached"])
            mr = max(rows, key=lambda r: r["reliability"])
            if best is None:
                self.summary_lbl.config(text="No reachable resolvers so far…")
            else:
                def part(r, key, unit):
                    return f"{L(r)} ({key(r):.1f} {unit})" if r else "—"
                self.summary_lbl.config(text=(
                    f"Best overall: {part(best, score_of, '')}   ·   "
                    f"Fastest cached: {part(fc, lambda r: r['cached'], 'ms')}   ·   "
                    f"Fastest uncached: {part(fu, lambda r: r['uncached'], 'ms')}   ·   "
                    f"Most reliable: {L(mr)} ({mr['reliability']:.1f} %)"))
            blocked = [r for r in rows
                       if "(system)" not in self.labels.get(r["server"], "")
                       and r["reliability"] == 0.0]
            reachable = [r for r in rows if r["reliability"] > 0.0]
            if blocked and len(blocked) == len(rows) - len(reachable) and blocked:
                self.hint_lbl.config(text=(
                    "⚠ All public resolvers unreachable (0 replies) — outbound UDP/53 "
                    "appears blocked by a VPN/firewall. Only the system resolver is testable."))

    def _on_close(self):
        self.stop_event.set()
        self.destroy()


def main():
    ap = argparse.ArgumentParser(description="DNS Benchmark GUI")
    ap.add_argument("--samples", type=int, default=40)
    ap.add_argument("--timeout", type=float, default=2.0)
    ap.add_argument("--auto", action="store_true", help="start benchmark on launch")
    opts = ap.parse_args()
    Dashboard(opts).mainloop()


if __name__ == "__main__":
    main()
