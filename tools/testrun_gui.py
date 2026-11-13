"""Test-run recorder: flow + pressure over time, exported as CSV.

Needs the test-run firmware (src/main.cpp). The board is the source of truth
for runs: start/stop here (sends 's' / 'x') or hold the board's button 1 s -
either way this tool records while the board reports RUN.

Each serial line looks like:
  12.30 Hz  21.50 L/min  total 1.234 L  P1 45.20 psi  P2 3.10 psi
  (1.234V OK, 0.800V OK)  peak P1 47.00 P2 3.20  t 123.500  RUN 4 12.000
"""
import csv, re, time, queue, threading, tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import ttk, filedialog, messagebox
import numpy as np, serial, serial.tools.list_ports
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

LINE = re.compile(
    r"([\d.]+)\s*Hz\s+([\d.]+)\s*L/min\s+total\s+([\d.]+)\s*L\s+"
    r"P1\s+(-?[\d.]+)\s*psi\s+P2\s+(-?[\d.]+)\s*psi\s+"
    r"\(\s*([\d.]+)V\s+([A-Z ]+?),\s*([\d.]+)V\s+([A-Z ]+?)\).*?"
    r"\bt\s+([\d.]+)\s+(?:RUN\s+(\d+)\s+([\d.]+)|IDLE)")
DEFAULT_DIR = Path(__file__).resolve().parent.parent / "data" / "runs"
COLUMNS = ["run", "label", "run_time_s", "device_time_s", "pc_time", "flow_hz", "flow_L_per_min",
           "run_volume_L", "p1_psi", "p2_psi", "p1_V", "p2_V", "p1_status", "p2_status"]


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Test run recorder")
        self.geometry("1250x820")
        self.ser = None
        self.q = queue.Queue()
        self.live = []          # recent samples (dicts), for the live view
        self.rec = []           # samples of the current / last run
        self.run = None         # run number being recorded
        self.recording = False
        self.saved = True
        self.last_draw = 0
        self.dirty = False
        self.build()
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
        ttk.Button(top, text="Set", command=self.set_interval).pack(side="left", padx=2)
        ttk.Label(top, text="Live window (s)").pack(side="left", padx=(20, 2))
        self.win = tk.IntVar(value=60)
        ttk.Spinbox(top, from_=10, to=3600, increment=10, width=6, textvariable=self.win).pack(side="left")
        ttk.Label(top, text="View").pack(side="left", padx=(20, 2))
        self.view = tk.StringVar(value="Live")
        for v in ("Live", "Run"):
            ttk.Radiobutton(top, text=v, value=v, variable=self.view, command=self.redraw).pack(side="left")

        mid = ttk.Frame(self)
        mid.pack(fill="both", expand=True, padx=8)
        left = ttk.Frame(mid)
        left.pack(side="left", fill="y")

        live = ttk.LabelFrame(left, text="Live")
        live.pack(fill="x", pady=4)
        self.state = tk.StringVar(value="Not connected")
        ttk.Label(live, textvariable=self.state, font=("Consolas", 12, "bold")).pack(anchor="w", padx=6, pady=(4, 0))
        self.big = tk.StringVar(value="--.--")
        ttk.Label(live, textvariable=self.big, font=("Consolas", 36, "bold")).pack(padx=10)
        ttk.Label(live, text="L/min").pack()
        self.detail = tk.StringVar(value="")
        ttk.Label(live, textvariable=self.detail, font=("Consolas", 11), justify="left").pack(anchor="w", padx=6, pady=4)

        run = ttk.LabelFrame(left, text="Test run")
        run.pack(fill="x", pady=4)
        row = ttk.Frame(run)
        row.pack(fill="x", padx=4, pady=2)
        ttk.Label(row, text="Label").pack(side="left")
        self.label = ttk.Entry(row, width=24)
        self.label.pack(side="left", padx=4, fill="x", expand=True)
        row = ttk.Frame(run)
        row.pack(fill="x", padx=4, pady=2)
        self.startbtn = ttk.Button(row, text="Start run", command=lambda: self.send("s"))
        self.startbtn.pack(side="left", expand=True, fill="x")
        ttk.Button(row, text="Stop run", command=lambda: self.send("x")).pack(side="left", expand=True, fill="x")
        ttk.Button(run, text="Zero pressure (open to air)", command=self.zero).pack(fill="x", padx=4, pady=2)
        self.autosave = tk.BooleanVar(value=True)
        ttk.Checkbutton(run, text="Autosave CSV when run stops", variable=self.autosave).pack(anchor="w", padx=4)
        self.folder = tk.StringVar(value=str(DEFAULT_DIR))
        row = ttk.Frame(run)
        row.pack(fill="x", padx=4, pady=2)
        ttk.Entry(row, textvariable=self.folder, width=28).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="...", width=3, command=self.pick_folder).pack(side="left")
        row = ttk.Frame(run)
        row.pack(fill="x", padx=4, pady=(2, 4))
        ttk.Button(row, text="Save CSV as...", command=self.save_as).pack(side="left", expand=True, fill="x")
        ttk.Button(row, text="Clear run", command=self.clear).pack(side="left", expand=True, fill="x")

        st = ttk.LabelFrame(left, text="Run stats")
        st.pack(fill="both", expand=True, pady=4)
        self.stats = tk.StringVar(value="No run yet")
        ttk.Label(st, textvariable=self.stats, font=("Consolas", 10), justify="left").pack(anchor="w", padx=6, pady=4)

        right = ttk.Frame(mid)
        right.pack(side="left", fill="both", expand=True, padx=8)
        self.fig = Figure(figsize=(7, 6))
        self.axf = self.fig.add_subplot(311)
        self.ax1 = self.fig.add_subplot(312, sharex=self.axf)
        self.ax2 = self.fig.add_subplot(313, sharex=self.axf)
        self.canvas = FigureCanvasTkAgg(self.fig, right)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

        self.log = tk.Text(self, height=6, font=("Consolas", 9))
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
        self.state.set("Connected - waiting for data")
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

    def set_interval(self):
        self.send(f"i {self.interval.get()}")

    def zero(self):
        if self.recording:
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
             "p2_V": float(g[7]), "p2_status": g[8].strip(), "device_time_s": float(g[9]),
             "pc_time": datetime.now().isoformat(timespec="milliseconds")}
        run = int(g[10]) if g[10] else None
        self.live.append(s)
        del self.live[:-20000]

        if run is not None:
            if run != self.run or not self.recording:
                self.begin_run(run, float(g[11]))
            s = dict(s, run=run, label=self.run_label, run_time_s=float(g[11]))
            self.rec.append(s)
            self.saved = False
        elif self.recording:
            self.end_run()

        st = f"RUN {run}  {float(g[11]):.0f} s  ({len(self.rec)} samples)" if run is not None else "IDLE"
        self.state.set(st)
        self.big.set(f"{s['flow_L_per_min']:5.2f}")
        self.detail.set(f"Flow  {s['flow_hz']:6.2f} Hz\n"
                        f"Vol   {s['run_volume_L']:6.3f} L\n"
                        f"P1    {s['p1_psi']:6.2f} psi  {s['p1_status']}\n"
                        f"P2    {s['p2_psi']:6.2f} psi  {s['p2_status']}")
        self.dirty = True

    def begin_run(self, run, run_t):
        if self.rec and not self.saved:
            self.say(f"Run {self.run} was not saved - it is being replaced by run {run}.")
        self.run = run
        self.run_label = self.label.get().strip()
        self.rec = []
        self.recording = True
        self.saved = False
        self.view.set("Run")
        self.startbtn.config(state="disabled")
        self.say(f"Recording run {run}" + (f" ({self.run_label})" if self.run_label else ""))
        if run_t > 2 * self.interval.get() / 1000 + 1:
            self.say(f"  note: joined run {run} already in progress at {run_t:.1f} s")

    def end_run(self):
        self.recording = False
        self.startbtn.config(state="normal")
        self.say(f"Run {self.run} ended: {len(self.rec)} samples")
        if self.autosave.get() and self.rec:
            folder = Path(self.folder.get())
            try:
                folder.mkdir(parents=True, exist_ok=True)
                self.write_csv(folder / self.default_name())
            except OSError as e:
                messagebox.showerror("Autosave", f"Could not save to {folder}:\n{e}")
        self.update_stats()

    def default_name(self):
        lab = re.sub(r"[^A-Za-z0-9_-]+", "_", getattr(self, "run_label", "")).strip("_")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return f"run{self.run:03d}{'_' + lab if lab else ''}_{stamp}.csv"

    def write_csv(self, path):
        with open(path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=COLUMNS)
            w.writeheader()
            for s in self.rec:
                w.writerow({k: s[k] for k in COLUMNS})
        self.saved = True
        self.say(f"Saved {len(self.rec)} samples to {path}")
        if not self.recording:
            self.update_stats()

    def save_as(self):
        if not self.rec:
            messagebox.showinfo("Save", "No run recorded yet.")
            return
        f = filedialog.asksaveasfilename(defaultextension=".csv", initialdir=self.folder.get(),
                                         initialfile=self.default_name())
        if f:
            self.write_csv(f)

    def pick_folder(self):
        d = filedialog.askdirectory(initialdir=self.folder.get())
        if d:
            self.folder.set(d)

    def clear(self):
        if self.recording:
            messagebox.showinfo("Clear", "Stop the run first.")
            return
        if self.rec and not self.saved and not messagebox.askyesno("Clear", "This run is not saved. Clear anyway?"):
            return
        self.rec = []
        self.saved = True
        self.stats.set("No run yet")
        self.view.set("Live")
        self.redraw()

    # ---------------- Plot + stats ----------------
    def redraw(self):
        self.last_draw = time.time()
        self.dirty = False
        for ax in (self.axf, self.ax1, self.ax2):
            ax.clear()
            ax.grid(True)
        self.axf.set_ylabel("Flow (L/min)")
        self.ax1.set_ylabel("P1 (psi)")
        self.ax2.set_ylabel("P2 (psi)")
        if self.view.get() == "Run" and self.rec:
            d = self.rec
            t = np.array([s["run_time_s"] for s in d])
            self.axf.set_title(f"Run {self.run}" + (" (recording)" if self.recording else ""))
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
        self.axf.plot(t, [s["flow_L_per_min"] for s in d], color="C0")
        self.ax1.plot(t, [s["p1_psi"] for s in d], color="C1")
        self.ax2.plot(t, [s["p2_psi"] for s in d], color="C2")
        self.fig.tight_layout()
        self.canvas.draw()
        if self.recording:
            self.update_stats()

    def update_stats(self):
        if len(self.rec) < 2:
            return
        t = np.array([s["run_time_s"] for s in self.rec])
        f = np.array([s["flow_L_per_min"] for s in self.rec])
        p1 = np.array([s["p1_psi"] for s in self.rec])
        p2 = np.array([s["p2_psi"] for s in self.rec])
        bad = sum(1 for s in self.rec if s["p1_status"] != "OK" or s["p2_status"] != "OK")
        dt = np.diff(t)
        gaps = int(np.sum(dt > 1.5 * np.median(dt))) if len(dt) else 0
        txt = (f"Run       {self.run}\n"
               f"Duration  {t[-1]:.1f} s\n"
               f"Samples   {len(t)}\n"
               f"Volume    {self.rec[-1]['run_volume_L']:.3f} L\n"
               f"Flow      mean {f.mean():.2f}  max {f.max():.2f} L/min\n"
               f"P1        mean {p1.mean():.2f}  max {p1.max():.2f} psi\n"
               f"P2        mean {p2.mean():.2f}  max {p2.max():.2f} psi")
        if bad:
            txt += f"\n! {bad} samples with sensor status not OK"
        if gaps:
            txt += f"\n! {gaps} gaps in timing (missed samples)"
        if not self.recording:
            txt += "\nSaved" if self.saved else "\nNOT SAVED"
        self.stats.set(txt)

    def on_close(self):
        if self.recording and not messagebox.askyesno("Quit", "A run is being recorded. Quit anyway?\n(The board keeps running.)"):
            return
        if not self.recording and self.rec and not self.saved and \
                not messagebox.askyesno("Quit", "The last run is not saved. Quit anyway?"):
            return
        if self.ser:
            self.ser.close()
        self.destroy()


App().mainloop()
