"""Pressure sensor calibration helper.

Works with the normal monitor firmware (src/main.cpp) - no reflash needed.
It reads the raw sensor voltages from the serial line, e.g.
    ... P1 4.97 psi  P2 0.89 psi  (0.461V OK, 0.448V OK)

Procedure:
  1. Connect to the XIAO's COM port.
  2. Pick the sensor (P1, P2, or both if they share the same manifold).
  3. Set a pressure, wait for the reading to settle (watch "Noise"),
     type the reference gauge reading, press "Capture point".
     Start with 0 psi (open to air), then 3+ points spread over the range.
  4. Press "Fit" - shows psi = gain * V + offset, R^2, and per-point error.
  5. Save points (CSV) and copy the firmware constants from the log.
"""
import re, time, queue, threading, tkinter as tk
from tkinter import ttk, filedialog, messagebox
import numpy as np, serial, serial.tools.list_ports
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

# "(0.461V OK, 0.448V OK)" - status can be OK, HIGH or NO SIG
VOLTS = re.compile(r"\(\s*([\d.]+)V\s+([A-Z ]+?),\s*([\d.]+)V\s+([A-Z ]+?)\)")
ADC_MAX_V = 3.0          # ESP32-C3 ADC flattens out above ~3.0-3.1 V
FULLSCALE = {"P1": 100.0, "P2": 20.0}
# Reference units -> psi. Water column uses water at ~20 C (998.2 kg/m^3).
UNITS = {"psi": 1.0, "cm H2O": 0.0141978, "in H2O": 0.0360625}


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Pressure calibration")
        self.geometry("1150x760")
        self.ser = None
        self.q = queue.Queue()
        self.recent = {"P1": [], "P2": []}   # last readings (V)
        self.status = {"P1": "--", "P2": "--"}
        self.points = []                      # dicts: sensor, ref_psi, mean_v, std_mv, n, time
        self.capture = None                   # active capture state
        self.fits = {}                        # sensor -> coefficients
        self.build()
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
        ttk.Label(top, text="Fit").pack(side="left", padx=(20, 2))
        self.deg = tk.StringVar(value="Linear")
        ttk.Combobox(top, width=10, state="readonly", textvariable=self.deg,
                     values=["Linear", "Quadratic"]).pack(side="left")

        mid = ttk.Frame(self)
        mid.pack(fill="both", expand=True, padx=8)
        left = ttk.Frame(mid)
        left.pack(side="left", fill="y")

        live = ttk.LabelFrame(left, text="Live voltage")
        live.pack(fill="x", pady=4)
        self.livev = {}
        for s in ("P1", "P2"):
            v = tk.StringVar(value=f"{s}  --.---- V")
            ttk.Label(live, textvariable=v, font=("Consolas", 13)).pack(anchor="w", padx=6)
            self.livev[s] = v
        ttk.Label(live, text="Noise = std dev of last 10 readings", font=("Segoe UI", 8)).pack(anchor="w", padx=6, pady=(0, 4))

        cap = ttk.LabelFrame(left, text="Capture point")
        cap.pack(fill="x", pady=4)
        row = ttk.Frame(cap)
        row.pack(fill="x", padx=4, pady=2)
        self.sensor = tk.StringVar(value="P1")
        for s in ("P1", "P2", "Both"):
            ttk.Radiobutton(row, text=s, value=s, variable=self.sensor).pack(side="left")
        row = ttk.Frame(cap)
        row.pack(fill="x", padx=4, pady=2)
        ttk.Label(row, text="Reference").pack(side="left")
        self.ref = ttk.Entry(row, width=9)
        self.ref.pack(side="left", padx=4)
        self.ref.bind("<Return>", lambda e: self.start_capture())
        self.unit = tk.StringVar(value="psi")
        ttk.Combobox(row, width=7, state="readonly", textvariable=self.unit,
                     values=list(UNITS)).pack(side="left")
        ttk.Label(cap, text="cm/in H2O = water height above the sensor port",
                  font=("Segoe UI", 8)).pack(anchor="w", padx=4)
        row = ttk.Frame(cap)
        row.pack(fill="x", padx=4, pady=2)
        ttk.Label(row, text="Average readings").pack(side="left")
        self.navg = tk.IntVar(value=10)
        ttk.Spinbox(row, from_=2, to=120, width=5, textvariable=self.navg).pack(side="left", padx=4)
        ttk.Label(row, text="(0.5 s each)").pack(side="left")
        self.capbtn = ttk.Button(cap, text="Capture point", command=self.start_capture)
        self.capbtn.pack(fill="x", padx=4, pady=4)
        self.capmsg = tk.StringVar(value="")
        ttk.Label(cap, textvariable=self.capmsg).pack(anchor="w", padx=4)

        pts = ttk.LabelFrame(left, text="Points")
        pts.pack(fill="both", expand=True, pady=4)
        cols = ("sensor", "ref", "volts", "noise", "n")
        self.tree = ttk.Treeview(pts, columns=cols, show="headings", height=10)
        for c, txt, w in zip(cols, ("Sensor", "Ref psi", "Mean V", "Noise mV", "N"), (55, 65, 70, 70, 35)):
            self.tree.heading(c, text=txt)
            self.tree.column(c, width=w, anchor="e")
        self.tree.pack(fill="both", expand=True, padx=4, pady=2)
        row = ttk.Frame(pts)
        row.pack(fill="x", padx=4, pady=2)
        ttk.Button(row, text="Delete selected", command=self.delete_sel).pack(side="left", expand=True, fill="x")
        ttk.Button(row, text="Fit", command=self.fit).pack(side="left", expand=True, fill="x")
        row = ttk.Frame(pts)
        row.pack(fill="x", padx=4, pady=2)
        ttk.Button(row, text="Save CSV", command=self.save_csv).pack(side="left", expand=True, fill="x")
        ttk.Button(row, text="Load CSV", command=self.load_csv).pack(side="left", expand=True, fill="x")

        right = ttk.Frame(mid)
        right.pack(side="left", fill="both", expand=True, padx=8)
        self.fig = Figure(figsize=(6, 5))
        self.ax = self.fig.add_subplot(211)
        self.axr = self.fig.add_subplot(212)
        self.fig.tight_layout()
        self.canvas = FigureCanvasTkAgg(self.fig, right)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

        self.log = tk.Text(self, height=9, font=("Consolas", 9))
        self.log.pack(fill="x", padx=8, pady=6)
        self.redraw()

    def say(self, msg):
        self.log.insert("end", msg + "\n")
        self.log.see("end")

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
        threading.Thread(target=self.reader, args=(self.ser,), daemon=True).start()

    def reader(self, ser):
        buf = b""
        while ser.is_open:
            try:
                buf += ser.read(256)
            except Exception:
                return
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                self.q.put(line.decode(errors="ignore").rstrip())

    def poll(self):
        try:
            while True:
                m = VOLTS.search(self.q.get_nowait())
                if m:
                    self.on_reading(float(m.group(1)), m.group(2).strip(),
                                    float(m.group(3)), m.group(4).strip())
        except queue.Empty:
            pass
        self.after(100, self.poll)

    def on_reading(self, v1, s1, v2, s2):
        for s, v, st in (("P1", v1, s1), ("P2", v2, s2)):
            r = self.recent[s]
            r.append(v)
            del r[:-10]
            self.status[s] = st
            noise = np.std(r) * 1000 if len(r) > 1 else 0
            self.livev[s].set(f"{s}  {v:.3f} V  {st:<6}  noise {noise:4.1f} mV")
        c = self.capture
        if c:
            for s in c["sensors"]:
                v = v1 if s == "P1" else v2
                st = s1 if s == "P1" else s2
                if st != "OK" or v > ADC_MAX_V:
                    c["bad"].add(s)
                c["samples"][s].append(v)
            done = len(c["samples"][c["sensors"][0]])
            self.capmsg.set(f"Capturing... {done}/{c['n']}")
            if done >= c["n"]:
                self.finish_capture()

    # ---------------- Capture ----------------
    def start_capture(self):
        if self.capture:
            return
        if not (self.ser and self.ser.is_open):
            messagebox.showinfo("Capture", "Connect to the board first.")
            return
        try:
            raw = float(self.ref.get())
        except ValueError:
            messagebox.showerror("Capture", "Enter the reference reading (gauge psi or water height).")
            return
        ref = raw * UNITS[self.unit.get()]
        if self.unit.get() != "psi":
            self.say(f"{raw:g} {self.unit.get()} = {ref:.4f} psi")
        sensors = ["P1", "P2"] if self.sensor.get() == "Both" else [self.sensor.get()]
        for s in sensors:
            if ref > FULLSCALE[s] * 1.05:
                if not messagebox.askyesno("Capture", f"{ref} psi is above {s}'s {FULLSCALE[s]:.0f} psi range. Capture anyway?"):
                    return
        self.capture = {"ref": ref, "sensors": sensors, "n": max(2, self.navg.get()),
                        "samples": {s: [] for s in sensors}, "bad": set()}
        self.capbtn.config(state="disabled")

    def finish_capture(self):
        c, self.capture = self.capture, None
        self.capbtn.config(state="normal")
        self.capmsg.set("")
        for s in c["sensors"]:
            v = np.array(c["samples"][s])
            if s in c["bad"]:
                self.say(f"{s} @ {c['ref']} psi REJECTED: signal was not OK or above {ADC_MAX_V} V "
                         f"(ADC saturates). Mean {v.mean():.3f} V.")
                continue
            p = {"sensor": s, "ref_psi": c["ref"], "mean_v": float(v.mean()),
                 "std_mv": float(v.std() * 1000), "n": len(v), "time": time.strftime("%Y-%m-%d %H:%M:%S")}
            self.points.append(p)
            self.say(f"{s} @ {p['ref_psi']:.2f} psi -> {p['mean_v']:.4f} V (noise {p['std_mv']:.1f} mV, n={p['n']})")
            if p["std_mv"] > 10:
                self.say(f"  warning: noisy reading ({p['std_mv']:.1f} mV) - pressure may not have settled.")
        self.refresh_tree()
        self.redraw()

    def refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        for i, p in enumerate(self.points):
            self.tree.insert("", "end", iid=str(i), values=(
                p["sensor"], f"{p['ref_psi']:.2f}", f"{p['mean_v']:.4f}", f"{p['std_mv']:.1f}", p["n"]))

    def delete_sel(self):
        sel = {int(i) for i in self.tree.selection()}
        self.points = [p for i, p in enumerate(self.points) if i not in sel]
        self.fits = {}
        self.refresh_tree()
        self.redraw()

    # ---------------- Fit ----------------
    def fit(self):
        deg = 1 if self.deg.get() == "Linear" else 2
        self.fits = {}
        for s in ("P1", "P2"):
            pts = [p for p in self.points if p["sensor"] == s]
            if not pts:
                continue
            if len(pts) < deg + 1:
                self.say(f"{s}: need at least {deg + 1} points for a {self.deg.get().lower()} fit (have {len(pts)}).")
                continue
            v = np.array([p["mean_v"] for p in pts])
            y = np.array([p["ref_psi"] for p in pts])
            coef = np.polyfit(v, y, deg)
            pred = np.polyval(coef, v)
            res = y - pred
            ss_tot = np.sum((y - y.mean()) ** 2)
            r2 = 1 - np.sum(res ** 2) / ss_tot if ss_tot > 0 else float("nan")
            self.fits[s] = coef
            self.say(f"--- {s} {self.deg.get().lower()} fit, {len(pts)} points ---")
            if deg == 1:
                self.say(f"  psi = {coef[0]:.4f} * V + {coef[1]:.4f}")
                self.say(f"  zero-pressure voltage = {-coef[1] / coef[0]:.4f} V")
                self.say(f"  firmware: const float {s}_GAIN = {coef[0]:.5f}f; const float {s}_OFFSET = {coef[1]:.5f}f;")
            else:
                self.say(f"  psi = {coef[0]:.5f} * V^2 + {coef[1]:.5f} * V + {coef[2]:.5f}")
                self.say(f"  firmware: const float {s}_A = {coef[0]:.6f}f, {s}_B = {coef[1]:.6f}f, {s}_C = {coef[2]:.6f}f;")
            self.say(f"  R^2 = {r2:.5f}   RMS error = {np.sqrt(np.mean(res ** 2)):.3f} psi   "
                     f"max error = {np.max(np.abs(res)):.3f} psi ({np.max(np.abs(res)) / FULLSCALE[s] * 100:.2f}% FS)")
            if len(pts) <= deg + 1:
                self.say("  note: fit passes exactly through every point - add more points to see real error.")
            vmax = max(v)
            self.say(f"  valid only between {min(y):.1f} and {max(y):.1f} psi "
                     f"({min(v):.3f}-{vmax:.3f} V); do not extrapolate.")
        self.redraw()

    def redraw(self):
        self.ax.clear()
        self.axr.clear()
        self.ax.set_xlabel("Sensor voltage (V)")
        self.ax.set_ylabel("Reference pressure (psi)")
        self.ax.set_title("Calibration curve")
        self.axr.set_xlabel("Reference pressure (psi)")
        self.axr.set_ylabel("Error (psi)")
        self.axr.axhline(0, color="0.6", lw=0.8)
        for s, col in (("P1", "C0"), ("P2", "C1")):
            pts = [p for p in self.points if p["sensor"] == s]
            if not pts:
                continue
            v = np.array([p["mean_v"] for p in pts])
            y = np.array([p["ref_psi"] for p in pts])
            self.ax.plot(v, y, "o", color=col, label=s)
            if s in self.fits:
                xs = np.linspace(v.min(), v.max(), 100)
                self.ax.plot(xs, np.polyval(self.fits[s], xs), "-", color=col, label=f"{s} fit")
                self.axr.plot(y, y - np.polyval(self.fits[s], v), "o-", color=col, label=s)
        self.ax.grid(True)
        self.axr.grid(True)
        if self.points:
            self.ax.legend()
        if self.fits:
            self.axr.legend()
        self.fig.tight_layout()
        self.canvas.draw()

    # ---------------- Files ----------------
    def save_csv(self):
        if not self.points:
            messagebox.showinfo("Save", "No points yet.")
            return
        f = filedialog.asksaveasfilename(defaultextension=".csv", initialfile="pressure_calibration.csv")
        if not f:
            return
        with open(f, "w") as fh:
            fh.write("sensor,ref_psi,mean_V,std_mV,n,time\n")
            for p in self.points:
                fh.write(f"{p['sensor']},{p['ref_psi']:.3f},{p['mean_v']:.5f},{p['std_mv']:.2f},{p['n']},{p['time']}\n")
        self.say(f"Saved {len(self.points)} points to {f}")

    def load_csv(self):
        f = filedialog.askopenfilename(filetypes=[("CSV", "*.csv")])
        if not f:
            return
        try:
            with open(f) as fh:
                rows = [ln.strip().split(",") for ln in fh.readlines()[1:] if ln.strip()]
            self.points = [{"sensor": r[0], "ref_psi": float(r[1]), "mean_v": float(r[2]),
                            "std_mv": float(r[3]), "n": int(r[4]), "time": r[5] if len(r) > 5 else ""}
                           for r in rows]
        except (OSError, ValueError, IndexError) as e:
            messagebox.showerror("Load", f"Could not read {f}:\n{e}")
            return
        self.fits = {}
        self.refresh_tree()
        self.redraw()
        self.say(f"Loaded {len(self.points)} points from {f}")


App().mainloop()
