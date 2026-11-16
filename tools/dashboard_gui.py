"""Dashboard: live flow + P1 + P2, run recording, CSV export.

Needs the test-run firmware (src/main.cpp). Runs are the board's runs: Start /
Stop here sends 's' / 'x', or hold the board's button 1 s - either way the
dashboard records while the board reports RUN. Every run recorded this session
is listed; select one (or several) to view or export as CSV.

Each serial line looks like:
  12.30 Hz  21.50 L/min  total 1.234 L  P1 45.20 psi  P2 3.10 psi
  (1.234V OK, 0.800V OK)  peak P1 47.00 P2 3.20  t 123.500  RUN 4 12.000
"""
import csv, os, re, time, queue, threading, tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import ttk, filedialog, messagebox, font as tkfont
import numpy as np, serial, serial.tools.list_ports
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

LINE = re.compile(
    r"([\d.]+)\s*Hz\s+([\d.]+)\s*L/min\s+total\s+([\d.]+)\s*L\s+"
    r"P1\s+(-?[\d.]+)\s*psi\s+P2\s+(-?[\d.]+)\s*psi\s+"
    r"\(\s*([\d.]+)V\s+([A-Z ]+?),\s*([\d.]+)V\s+([A-Z ]+?)\)\s+"
    r"peak\s+P1\s+(-?[\d.]+)\s+P2\s+(-?[\d.]+)\s+"
    r"t\s+([\d.]+)\s+(?:RUN\s+(\d+)\s+([\d.]+)|IDLE)")
DEFAULT_DIR = Path(__file__).resolve().parent.parent / "data" / "runs"
COLUMNS = ["run", "label", "run_time_s", "device_time_s", "pc_time", "flow_hz", "flow_L_per_min",
           "run_volume_L", "p1_psi", "p2_psi", "p1_V", "p2_V", "p1_status", "p2_status"]
COLORS = {"flow": "#1f77b4", "p1": "#ff7f0e", "p2": "#2ca02c"}   # matplotlib C0, C1, C2
OK_FG, BAD_FG = "#2a7a2a", "#c62828"
HZ_CAL_MIN, HZ_CAL_MAX = 6.0, 18.4      # flow calibration range (README)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Flow + pressure dashboard")
        self.geometry("1400x900")
        self.ser = None
        self.q = queue.Queue()
        self.live = []          # recent samples (dicts), for the live view
        self.runs = {}          # tree id -> {"run", "label", "samples", "path"}
        self.cur = None         # tree id of the run being recorded
        self.next_id = 0
        self.last_draw = 0
        self.dirty = False
        self.build()
        try:
            self.state("zoomed")    # maximized (Windows)
        except tk.TclError:
            pass
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self.poll)

    # ---------------- UI ----------------
    def build(self):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=6)
        self.port = ttk.Combobox(top, width=12, values=self.ports())
        self.port.pack(side="left")
        if self.port["values"]:
            self.port.current(0)
        ttk.Button(top, text="Refresh", command=lambda: self.port.config(values=self.ports())).pack(side="left", padx=2)
        ttk.Button(top, text="Connect", command=self.connect).pack(side="left", padx=2)
        ttk.Label(top, text="Sample interval (ms)").pack(side="left", padx=(20, 2))
        self.interval = tk.IntVar(value=500)
        ttk.Spinbox(top, from_=100, to=10000, increment=100, width=6, textvariable=self.interval).pack(side="left")
        ttk.Button(top, text="Set", command=lambda: self.send(f"i {self.interval.get()}")).pack(side="left", padx=2)
        ttk.Label(top, text="Live window (s)").pack(side="left", padx=(20, 2))
        self.win = tk.IntVar(value=60)
        ttk.Spinbox(top, from_=10, to=3600, increment=10, width=6, textvariable=self.win).pack(side="left")
        ttk.Button(top, text="Zero pressure", command=self.zero).pack(side="left", padx=(20, 2))
        ttk.Button(top, text="Reset peaks", command=lambda: self.send("p")).pack(side="left", padx=2)
        self.conn = tk.StringVar(value="Not connected")
        ttk.Label(top, textvariable=self.conn, font=("Consolas", 12, "bold")).pack(side="left", padx=(20, 2))

        # Tiles: one per sensor
        tiles = ttk.Frame(self)
        tiles.pack(fill="x", padx=8)
        self.tile = {}
        for key, title, unit in (("flow", "Flow", "L/min"), ("p1", "P1  (100 psi sensor)", "psi"),
                                 ("p2", "P2  (20 psi sensor)", "psi")):
            f = ttk.LabelFrame(tiles, text=title)
            f.pack(side="left", fill="both", expand=True, padx=4, pady=4)
            row = ttk.Frame(f)
            row.pack(anchor="w", padx=10)
            big = tk.StringVar(value="--.--")
            ttk.Label(row, textvariable=big, font=("Consolas", 36, "bold"),
                      foreground=COLORS[key]).pack(side="left")
            ttk.Label(row, text=unit, font=("Consolas", 14)).pack(side="left", anchor="s", pady=(0, 8), padx=4)
            detail = tk.StringVar(value="")
            ttk.Label(f, textvariable=detail, font=("Consolas", 11), justify="left").pack(anchor="w", padx=10)
            status = ttk.Label(f, text="", font=("Consolas", 11, "bold"))
            status.pack(anchor="w", padx=10, pady=(0, 6))
            self.tile[key] = (big, detail, status)

        mid = ttk.Frame(self)
        mid.pack(fill="both", expand=True, padx=8)

        # Right column: recording + runs (packed first so the plot can't squeeze it)
        side = ttk.Frame(mid)
        side.pack(side="right", fill="y", padx=(8, 0))

        # Plots
        plots = ttk.Frame(mid)
        plots.pack(side="left", fill="both", expand=True)
        row = ttk.Frame(plots)
        row.pack(fill="x")
        ttk.Label(row, text="Plot").pack(side="left")
        self.view = tk.StringVar(value="Live")
        ttk.Radiobutton(row, text="Live", value="Live", variable=self.view, command=self.redraw).pack(side="left")
        ttk.Radiobutton(row, text="Selected run", value="Run", variable=self.view,
                        command=self.redraw).pack(side="left")
        self.fig = Figure(figsize=(7, 6))
        self.axf = self.fig.add_subplot(311)
        self.ax1 = self.fig.add_subplot(312, sharex=self.axf)
        self.ax2 = self.fig.add_subplot(313, sharex=self.axf)
        self.canvas = FigureCanvasTkAgg(self.fig, plots)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

        rec = ttk.LabelFrame(side, text="Record")
        rec.pack(fill="x", pady=4)
        row = ttk.Frame(rec)
        row.pack(fill="x", padx=4, pady=2)
        ttk.Label(row, text="Label").pack(side="left")
        self.label = ttk.Entry(row, width=30)
        self.label.pack(side="left", padx=4, fill="x", expand=True)
        self.recbtn = ttk.Button(rec, text="Start run", command=self.toggle_run)
        self.recbtn.pack(fill="x", padx=4, pady=2)
        self.recinfo = tk.StringVar(value="Not recording")
        ttk.Label(rec, textvariable=self.recinfo, font=("Consolas", 11)).pack(anchor="w", padx=6, pady=2)
        self.autosave = tk.BooleanVar(value=True)
        ttk.Checkbutton(rec, text="Autosave CSV when run stops", variable=self.autosave).pack(anchor="w", padx=4)
        self.folder = tk.StringVar(value=str(DEFAULT_DIR))
        row = ttk.Frame(rec)
        row.pack(fill="x", padx=4, pady=(2, 4))
        ttk.Entry(row, textvariable=self.folder, width=30).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="...", width=3, command=self.pick_folder).pack(side="left")
        ttk.Button(row, text="Open", width=5, command=self.open_folder).pack(side="left")

        runs = ttk.LabelFrame(side, text="Runs this session")
        runs.pack(fill="both", expand=True, pady=4)
        # size rows/columns from the font so the table survives display scaling
        line = tkfont.nametofont("TkDefaultFont").metrics("linespace")
        ttk.Style(self).configure("Treeview", rowheight=line + 6)
        px = self.winfo_fpixels("1i") / 96
        cols = ("run", "label", "dur", "vol", "p1", "p2", "saved")
        self.tree = ttk.Treeview(runs, columns=cols, show="headings", height=8, selectmode="extended")
        for c, h, w in zip(cols, ("Run", "Label", "Dur s", "Vol L", "P1 max", "P2 max", "CSV"),
                           (45, 110, 55, 60, 60, 60, 45)):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=int(w * px), minwidth=int(w * px), anchor="w" if c == "label" else "e", stretch=(c == "label"))
        self.tree.pack(fill="both", expand=True, padx=4, pady=4)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        row = ttk.Frame(runs)
        row.pack(fill="x", padx=4, pady=(0, 4))
        ttk.Button(row, text="Export CSV...", command=self.export).pack(side="left", expand=True, fill="x")
        ttk.Button(row, text="Remove", command=self.remove).pack(side="left", expand=True, fill="x")
        self.stats = tk.StringVar(value="No run selected")
        ttk.Label(runs, textvariable=self.stats, font=("Consolas", 10), justify="left").pack(anchor="w", padx=6, pady=4)

        self.log = tk.Text(self, height=5, font=("Consolas", 9))
        self.log.pack(fill="x", padx=8, pady=6)
        self.redraw()

    def say(self, msg):
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        if int(float(self.log.index("end"))) > 300:
            self.log.delete("1.0", "100.0")

    # ---------------- Serial ----------------
    def ports(self):
        return [p.device for p in serial.tools.list_ports.comports()]

    def connect(self):
        try:
            if self.ser:
                self.ser.close()
            self.ser = serial.Serial(self.port.get(), 115200, timeout=0.1)
        except Exception as e:
            messagebox.showerror("Serial", str(e))
            return
        self.say(f"Connected to {self.port.get()}")
        self.conn.set("Connected - waiting for data")
        threading.Thread(target=self.reader, args=(self.ser,), daemon=True).start()
        self.after(300, lambda: self.send("h"))

    def reader(self, ser):
        buf = b""
        while ser.is_open:
            try:
                buf += ser.read(256)
            except Exception:
                self.q.put("!! serial connection lost")
                return
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                self.q.put(line.decode(errors="ignore").rstrip())

    def send(self, cmd):
        if not (self.ser and self.ser.is_open):
            messagebox.showinfo("Serial", "Connect to the board first.")
            return False
        self.ser.write((cmd + "\n").encode())
        return True

    def toggle_run(self):
        self.send("x" if self.cur is not None else "s")

    def zero(self):
        if self.cur is not None:
            messagebox.showinfo("Zero", "Stop the run before zeroing.")
            return
        if messagebox.askyesno("Zero pressure", "Are both sensors open to air (0 psi)?\nThis replaces the saved zero."):
            self.send("z")

    # ---------------- Data ----------------
    def poll(self):
        try:
            while True:
                line = self.q.get_nowait()
                m = LINE.search(line)
                if m:
                    self.on_sample(m)
                elif line:
                    self.say(line)
        except queue.Empty:
            pass
        if self.dirty and time.time() - self.last_draw > 0.5:
            self.redraw()
        self.after(100, self.poll)

    def on_sample(self, m):
        g = m.groups()
        s = {"flow_hz": float(g[0]), "flow_L_per_min": float(g[1]), "run_volume_L": float(g[2]),
             "p1_psi": float(g[3]), "p2_psi": float(g[4]), "p1_V": float(g[5]), "p1_status": g[6].strip(),
             "p2_V": float(g[7]), "p2_status": g[8].strip(), "device_time_s": float(g[11]),
             "pc_time": datetime.now().isoformat(timespec="milliseconds")}
        peak1, peak2 = float(g[9]), float(g[10])
        run = int(g[12]) if g[12] else None
        run_t = float(g[13]) if g[13] else 0.0
        self.live.append(s)
        del self.live[:-20000]

        if run is not None:
            if self.cur is None or self.runs[self.cur]["run"] != run:
                if self.cur is not None:
                    self.end_run()
                self.begin_run(run, run_t)
            r = self.runs[self.cur]
            r["samples"].append(dict(s, run=run, label=r["label"], run_time_s=run_t))
            self.recinfo.set(f"RUN {run}  {int(run_t) // 60}:{int(run_t) % 60:02d}  "
                             f"({len(r['samples'])} samples)")
            self.conn.set(f"RUN {run}")
        else:
            if self.cur is not None:
                self.end_run()
            self.conn.set("IDLE")
        self.update_tiles(s, peak1, peak2)
        self.dirty = True

    def update_tiles(self, s, peak1, peak2):
        big, detail, status = self.tile["flow"]
        hz = s["flow_hz"]
        big.set(f"{s['flow_L_per_min']:6.2f}")
        detail.set(f"{hz:6.2f} Hz\nVolume {s['run_volume_L']:7.3f} L")
        if hz <= 0:
            status.config(text="no flow", foreground="gray")
        elif hz < HZ_CAL_MIN:
            status.config(text=f"~ below calibrated range (<{HZ_CAL_MIN:g} Hz)", foreground=BAD_FG)
        elif hz > HZ_CAL_MAX:
            status.config(text=f"~ extrapolated (>{HZ_CAL_MAX:g} Hz)", foreground=BAD_FG)
        else:
            status.config(text="in calibrated range", foreground=OK_FG)
        for key, peak in (("p1", peak1), ("p2", peak2)):
            big, detail, status = self.tile[key]
            big.set(f"{s[key + '_psi']:6.2f}")
            pk = f"{peak:6.2f} psi" if peak > -1e8 else "   --"
            detail.set(f"{s[key + '_V']:.3f} V\nPeak  {pk}")
            st = s[key + "_status"]
            status.config(text=st, foreground=OK_FG if st == "OK" else BAD_FG)

    def begin_run(self, run, run_t):
        iid = str(self.next_id)
        self.next_id += 1
        self.runs[iid] = {"run": run, "label": self.label.get().strip(), "samples": [], "path": None}
        self.cur = iid
        self.tree.insert("", "end", iid=iid)
        self.refresh_row(iid)
        self.tree.selection_set(iid)
        self.tree.see(iid)
        self.view.set("Run")
        self.recbtn.config(text="Stop run")
        lab = self.runs[iid]["label"]
        self.say(f"Recording run {run}" + (f" ({lab})" if lab else ""))
        if run_t > 2 * self.interval.get() / 1000 + 1:
            self.say(f"  note: joined run {run} already in progress at {run_t:.1f} s")

    def end_run(self):
        iid, self.cur = self.cur, None
        r = self.runs[iid]
        self.recbtn.config(text="Start run")
        self.recinfo.set("Not recording")
        self.say(f"Run {r['run']} ended: {len(r['samples'])} samples")
        if self.autosave.get() and r["samples"]:
            folder = Path(self.folder.get())
            try:
                folder.mkdir(parents=True, exist_ok=True)
                self.write_csv(iid, folder / self.default_name(r))
            except OSError as e:
                messagebox.showerror("Autosave", f"Could not save to {folder}:\n{e}")
        self.refresh_row(iid)
        self.show_stats()

    # ---------------- Runs list + export ----------------
    def refresh_row(self, iid):
        r = self.runs[iid]
        d = r["samples"]
        if d:
            vals = (r["run"], r["label"], f"{d[-1]['run_time_s']:.0f}", f"{d[-1]['run_volume_L']:.3f}",
                    f"{max(s['p1_psi'] for s in d):.1f}", f"{max(s['p2_psi'] for s in d):.1f}")
        else:
            vals = (r["run"], r["label"], "", "", "", "")
        saved = "rec" if iid == self.cur else ("yes" if r["path"] else "NO")
        self.tree.item(iid, values=vals + (saved,))

    def default_name(self, r):
        lab = re.sub(r"[^A-Za-z0-9_-]+", "_", r["label"]).strip("_")
        stamp = datetime.fromisoformat(r["samples"][0]["pc_time"]).strftime("%Y%m%d-%H%M%S")
        return f"run{r['run']:03d}{'_' + lab if lab else ''}_{stamp}.csv"

    def write_csv(self, iid, path):
        r = self.runs[iid]
        with open(path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=COLUMNS)
            w.writeheader()
            for s in r["samples"]:
                w.writerow({k: s[k] for k in COLUMNS})
        r["path"] = str(path)
        self.refresh_row(iid)
        self.say(f"Saved run {r['run']} ({len(r['samples'])} samples) to {path}")

    def selected(self, allow_current=False):
        return [i for i in self.tree.selection()
                if self.runs[i]["samples"] and (allow_current or i != self.cur)]

    def export(self):
        sel = self.selected()
        if not sel:
            messagebox.showinfo("Export", "Select one or more finished runs in the list.")
            return
        try:
            if len(sel) == 1:
                r = self.runs[sel[0]]
                f = filedialog.asksaveasfilename(defaultextension=".csv", initialdir=self.folder.get(),
                                                 initialfile=self.default_name(r),
                                                 filetypes=[("CSV", "*.csv"), ("All files", "*.*")])
                if f:
                    self.write_csv(sel[0], f)
            else:
                d = filedialog.askdirectory(initialdir=self.folder.get(), title=f"Folder for {len(sel)} runs")
                if d:
                    for iid in sel:
                        self.write_csv(iid, Path(d) / self.default_name(self.runs[iid]))
        except OSError as e:
            messagebox.showerror("Export", str(e))
        self.show_stats()

    def remove(self):
        sel = [i for i in self.tree.selection() if i != self.cur]
        if not sel:
            return
        unsaved = [self.runs[i]["run"] for i in sel if not self.runs[i]["path"] and self.runs[i]["samples"]]
        if unsaved and not messagebox.askyesno("Remove", f"Run(s) {unsaved} are not saved. Remove anyway?"):
            return
        for i in sel:
            self.tree.delete(i)
            del self.runs[i]
        self.show_stats()
        self.redraw()

    def pick_folder(self):
        d = filedialog.askdirectory(initialdir=self.folder.get())
        if d:
            self.folder.set(d)

    def open_folder(self):
        folder = Path(self.folder.get())
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(folder)   # Windows

    def on_select(self, _=None):
        if self.tree.selection():
            self.view.set("Run")
        self.show_stats()
        self.redraw()

    # ---------------- Plot + stats ----------------
    def plot_run(self):
        """Run shown in 'Selected run' view: first selected, else the current one."""
        sel = self.selected(allow_current=True)
        return sel[0] if sel else self.cur

    def redraw(self):
        self.last_draw = time.time()
        self.dirty = False
        if self.cur is not None:
            self.refresh_row(self.cur)
            if self.cur in self.tree.selection():
                self.show_stats()
        for ax in (self.axf, self.ax1, self.ax2):
            ax.clear()
            ax.grid(True)
        for ax in (self.axf, self.ax1):
            ax.tick_params(labelbottom=False)
        self.axf.set_ylabel("Flow (L/min)")
        self.ax1.set_ylabel("P1 (psi)")
        self.ax2.set_ylabel("P2 (psi)")
        iid = self.plot_run()
        if self.view.get() == "Run" and iid is not None and self.runs[iid]["samples"]:
            r = self.runs[iid]
            d = r["samples"]
            t = np.array([s["run_time_s"] for s in d])
            title = f"Run {r['run']}" + (f" - {r['label']}" if r["label"] else "")
            self.axf.set_title(title + (" (recording)" if iid == self.cur else ""))
            self.ax2.set_xlabel("Run time (s)")
        elif self.live:
            t_all = np.array([s["device_time_s"] for s in self.live])
            keep = t_all >= t_all[-1] - self.win.get()
            d = [s for s, k in zip(self.live, keep) if k]
            t = t_all[keep] - t_all[-1]
            self.axf.set_title(f"Live (last {self.win.get()} s)")
            self.ax2.set_xlabel("Time (s, 0 = now)")
        else:
            self.canvas.draw()
            return
        self.axf.plot(t, [s["flow_L_per_min"] for s in d], color=COLORS["flow"])
        self.ax1.plot(t, [s["p1_psi"] for s in d], color=COLORS["p1"])
        self.ax2.plot(t, [s["p2_psi"] for s in d], color=COLORS["p2"])
        self.fig.tight_layout()
        self.canvas.draw()

    def show_stats(self):
        iid = self.plot_run()
        if iid is None or len(self.runs[iid]["samples"]) < 2:
            self.stats.set("No run selected")
            return
        r = self.runs[iid]
        d = r["samples"]
        t = np.array([s["run_time_s"] for s in d])
        f = np.array([s["flow_L_per_min"] for s in d])
        p1 = np.array([s["p1_psi"] for s in d])
        p2 = np.array([s["p2_psi"] for s in d])
        bad = sum(1 for s in d if s["p1_status"] != "OK" or s["p2_status"] != "OK")
        dt = np.diff(t)
        gaps = int(np.sum(dt > 1.5 * np.median(dt)))
        txt = (f"Run       {r['run']}" + (f"  ({r['label']})" if r["label"] else "") + "\n"
               f"Duration  {t[-1]:.1f} s   Samples {len(t)}\n"
               f"Volume    {d[-1]['run_volume_L']:.3f} L\n"
               f"Flow      mean {f.mean():6.2f}  max {f.max():6.2f} L/min\n"
               f"P1        mean {p1.mean():6.2f}  max {p1.max():6.2f} psi\n"
               f"P2        mean {p2.mean():6.2f}  max {p2.max():6.2f} psi")
        if bad:
            txt += f"\n! {bad} samples with sensor status not OK"
        if gaps:
            txt += f"\n! {gaps} gaps in timing (missed samples)"
        if iid != self.cur:
            txt += f"\nSaved: {Path(r['path']).name}" if r["path"] else "\nNOT SAVED"
        self.stats.set(txt)

    def on_close(self):
        if self.cur is not None and not messagebox.askyesno(
                "Quit", "A run is being recorded. Quit anyway?\n(The board keeps running.)"):
            return
        unsaved = [r["run"] for i, r in self.runs.items() if i != self.cur and r["samples"] and not r["path"]]
        if unsaved and not messagebox.askyesno("Quit", f"Run(s) {unsaved} are not saved. Quit anyway?"):
            return
        if self.ser:
            self.ser.close()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
