# Deveng200 — Flow + Pressure Monitor (XIAO ESP32-C3)

Embedded firmware for a Seeed XIAO ESP32-C3 that measures water flow and two pressures for a water-treatment test setup. Live values show on a small OLED and over USB serial. **Flow is calibrated. Pressure is not calibrated yet** - that is the main handoff for the next person.

## Repo layout

```
├── platformio.ini          PlatformIO env for seeed_xiao_esp32c3
├── src/main.cpp            Firmware (flow + pressure + OLED + serial)
├── tools/
│   ├── flow_monitor_gui.py Live plot / record from the monitor firmware
│   └── flowcal_gui.py      Gravimetric flow-calibration helper GUI
├── data/
│   └── CALIBRATION_FLOW_TRIAL_1.csv   13-point flow calibration runs
├── requirements.txt        Python deps for the GUIs
└── README.md
```

## Hardware

| Part | Detail |
|---|---|
| Board | Seeed XIAO ESP32-C3 (PlatformIO `seeed_xiao_esp32c3`, Arduino) |
| Display | SSD1306 128×32 OLED over I2C, mounted rotated 180° (`U8G2_R2`) |
| Flow | Pulse turbine, signal → GPIO21, internal pull-up, falling-edge ISR |
| Pressure | Two ratiometric transducers (red = supply, black = GND, green = signal) |
| Button | GPIO6 (D4) → GND, internal pull-up, 200 ms debounce in ISR |

### Pressure sensors

- **Model:** Flylin stainless 1/8" NPT transducers, rated 5 V supply, 0.5–4.5 V output, ±2% FS
- **P1:** 100 psi, GPIO3 (D1)
- **P2:** 20 psi, GPIO4 (D2)
- **Powered from the XIAO 3V3 pin, no dividers** (supply measured 3.3 V). Output can't exceed the supply, so the ADC pins are safe at any pressure.
- Running at 3.3 V is outside the datasheet. Zero is measured (≈0.46 V, not the nominal 0.33 V); the slope is the datasheet 80%-of-supply span scaled to 3.3 V and has only been sanity-checked with a digital bike-pump gauge, not fitted.
- If the sensors are ever moved to 5 V, the green output (up to ~4.5 V) **must** go through a divider before the ADC pin, and `SUPPLY_V` / zero / divider ratio in `main.cpp` must change.

### Pin map (GPIO)

| Function | GPIO | XIAO label |
|---|---|---|
| OLED SDA | 20 | D7 |
| OLED SCL | 8 | D8 |
| Button | 6 | D4 |
| Pressure 1 | 3 | D1 |
| Pressure 2 | 4 | D2 |
| Flow signal | 21 | D6 |

Notes:

- GPIO2 (D0) was unreliable for flow during bring-up → flow is on GPIO21.
- Only D0–D3 (GPIO2–GPIO5) have analog input on this board.
- Avoid GPIO9 (boot strap pin).

## Build & flash (firmware)

Requires [PlatformIO](https://platformio.org/).

```bash
pio run -t upload
pio device monitor
```

Serial: **115200** baud.

**Windows path tip:** linking can fail with `collect2.exe: CreateProcess: No such file or directory` if the project lives under a long OneDrive path. Use a short path (e.g. `C:\fm`) and optionally `PLATFORMIO_CORE_DIR=C:\.platformio`.

If the OLED stays blank, `Wire.setPins` may not apply on the installed core. Fall back to software I2C:

```cpp
U8G2_SSD1306_128X32_UNIVISION_F_SW_I2C u8g2(U8G2_R2, PIN_SCL, PIN_SDA, U8X8_PIN_NONE);
```

and remove the three `Wire` lines in `setup()`.

## Run the live GUI

```bash
pip install -r requirements.txt
python tools/flow_monitor_gui.py
```

Pick the XIAO COM port → Connect. The GUI plots L/min and can record a CSV.

## Test runs (flow + pressure recording)

```bash
python tools/testrun_gui.py
```

Pick the COM port → Connect, type a label, then **Start run** / **Stop run**. Holding the board's button for 1 s also starts/stops a run, and the tool follows the board either way. A run resets total volume and peaks and starts a run timer (OLED page 1 shows `RUN n m:ss`). Run numbers are saved on the board, so they stay unique across reboots.

The tool plots flow, P1 and P2 live, shows run stats, and on stop autosaves `data/runs/run###_<label>_<date-time>.csv` (columns: `run, label, run_time_s, device_time_s, pc_time, flow_hz, flow_L_per_min, run_volume_L, p1_psi, p2_psi, p1_V, p2_V, p1_status, p2_status`). `run_time_s` comes from the board's clock, so PC lag doesn't affect timing.

Sample interval: **Set** in the tool or serial `i <ms>` (100–10000, default 500; resets to 500 on reboot). Short intervals make flow coarse: at 10 Hz a 200 ms sample holds only ~2 pulses.

`flowcal_gui.py` talks to a **separate calibration sketch** (not in this repo). Use it only if you re-run gravimetric flow calibration with that sketch loaded.

## Behavior (current firmware)

- Updates every **500 ms**. Flow Hz = pulse delta / actual elapsed time.
- Total volume = integral of L/min over time. Reset with serial `r` or on reboot.
- Button cycles OLED pages:
  - **Page 0:** flow summary (Hz, L/min, total, P1/P2 psi)
  - **Page 1:** pressure volts, status, psi
- Pressure status: `NO SIG` (&lt; 0.15 V), `HIGH` (&gt; 3.05 V), `OK`
- Serial line format (keep the **flow** prefix stable for the GUI regex):

```text
12.30 Hz  21.50 L/min  total 1.234 L  P1 45.20 psi  P2 3.10 psi  (1.234V OK, 0.800V OK)
```

GUI regex (flow only today):

```text
([\d.]+)\s*Hz\s+([\d.]+)\s*L/min\s+total\s+([\d.]+)\s*L
```

## Flow calibration (done)

Gravimetric method: capture pulses between two button presses, weigh water (grams / 0.998 g/mL), time with a stopwatch, compute Hz and pulses per liter.

- 13 runs, **6.4–18.4 Hz** (see `data/CALIBRATION_FLOW_TRIAL_1.csv`)
- One outlier (~8.48 Hz, 39.35 pulses/L) was deleted
- Fit used in firmware:

```text
pulses per liter = 0.31824 * Hz + 28.929
L/min = Hz * 60 / pulses per liter
```

- Fit error on the 13 points: ~**3.1% RMS**, ~**6.4% max**
- Most scatter is stopwatch error on short runs (1 kg ≈ 3–5 s at these flows)
- Below **6 Hz** is untested; OLED appends `~` after L/min there
- Above ~18.4 Hz the line **extrapolates** — verify before relying on it
- Valid only for **this** sensor, tubing, and orientation — recalibrate if any change
- A piecewise-linear table was tried and rejected (jumpy given per-run scatter)

## Pressure calibration (zeroed; slope not yet fitted)

Formula in `src/main.cpp`:

```cpp
psi = (v - zeroV) / (0.80f * SUPPLY_V) * fullScale;
```

Current configuration (2026-11-13):

| Sensor | Zero voltage (open to air) | Slope (nominal) |
|---|---|---|
| P1 (100 psi) | 0.4606 V | 37.9 psi/V |
| P2 (20 psi) | 0.4492 V | 7.58 psi/V |

Zero values are the defaults in `main.cpp` and are also saved on the board (NVS).

Serial commands (115200 baud, newline-terminated):

| Command | Action |
|---|---|
| `z` | Zero both sensors at the current reading (sensors open to air), save to flash |
| `zc` | Clear saved zero, use the defaults in `main.cpp` |
| `p` | Reset peak pressures |
| `r` | Reset total flow volume |
| `s` / `x` | Start / stop a test run |
| `i <ms>` | Sample interval, 100–10000 ms |
| `h` | Print help and current settings |

Peak hold: the serial line ends with `peak P1 … P2 …`, and OLED page 3 (button) shows max psi since boot / `p` / `z`. Use it to find the system's maximum pressure. Check with P1 before exposing P2 (20 psi) to an unknown pressure.

### Calibration tool

```bash
python tools/pressurecal_gui.py
```

Reads raw voltages from the normal firmware (no reflash). Capture points against a reference (psi, or water-column height in cm/in H2O: 70.3 cm ≈ 1 psi), then **Fit** gives gain/offset, R², and error per point, and **Save CSV** stores the points. If a fitted gain differs noticeably from the nominal slope above, update the firmware.

Suggested procedure for a full calibration:

1. Measure real supply voltage with a multimeter → set `SUPPLY_V`.
2. Check green output at zero pressure (~10% of supply).
3. Zero both sensors at atmospheric pressure (offset).
4. Check span against a reference gauge at **≥3** pressures → apply gain.
5. Prefer mid-range pressures; ESP32 ADC is less linear near the ends.

Expected accuracy after calibration (rough): **±1 psi** on the 100 psi sensor, **±0.3 psi** on the 20 psi sensor.

### Suggested firmware / tooling next steps

1. Move constants into `config.h`; store pressure zero/gain in **Preferences (NVS)**.
2. Add serial commands for pressure zeroing and span calibration.
3. Add pressure columns (and plots) to `tools/flow_monitor_gui.py`.
4. Add a CSV logging mode with timestamped flow **and** pressure.
5. Re-run flow calibration at low flow (&lt;6 Hz) with longer runs (≥5 kg) to cut timing error.
6. Fix GUI “Reset device total” to send serial `r` (firmware already supports it; button currently only shows a message).

## Known issues

- Windows long-path link failures under OneDrive — use a short project path.
- `volatile` increment warning on `pulseCount++` is harmless.
- Monitor GUI does not parse pressure yet (pressure is already on the serial line).

## License / ownership

Internal lab / course handoff for DEVENG 200 water-treatment test setup. Iterate freely for that purpose.
