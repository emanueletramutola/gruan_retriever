import netCDF4 as nc
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
import numpy as np
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import multiprocessing as mp
import os
import pickle
import hashlib
import threading
import queue
from collections import defaultdict

# --- Configuration ---
CACHE_DIR = os.path.expanduser("~/.gruan_viewer_cache")
BATCH_SIZE = 50
NUM_WORKERS = min(mp.cpu_count(), 8)
CATALOG_VERSION = 2

# Name of the NetCDF variable containing the observed value.
# If your file uses a different name, change it here.
VALUE_VAR_NAME = "observation_value"

# To speed up testing: limit the scan to the first N launches instead of
# processing the whole dataset. Set to None to process everything
# (needed to see ALL variables present in the file, since the file
# appears to be organized in contiguous blocks per variable).
MAX_LAUNCHES = None

# 'spawn' multiprocessing context: prevents worker processes from
# inheriting the Tk/X11 GUI state (a common cause of the terminal
# hanging on exit, requiring CTRL+C to regain control).
MP_CTX = mp.get_context('spawn')


def get_cache_path(file_path):
    """A separate cache file for each opened NetCDF file (based on its path)."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    h = hashlib.md5(os.path.abspath(file_path).encode('utf-8')).hexdigest()[:16]
    return os.path.join(CACHE_DIR, f"catalog_{h}.pkl")


def _clean_str(s):
    """Removes leftover NUL/control characters from strings decoded from the
    NetCDF file (a common cause of the 'Glyph 0 missing' warning in matplotlib)."""
    return s.replace('\x00', '').strip()


def get_station_name(raw):
    """Robust decoding of the station name."""
    if isinstance(raw, bytes):
        return _clean_str(raw.decode('utf-8', errors='replace'))
    elif isinstance(raw, np.ndarray):
        if raw.dtype.type is np.bytes_:
            return _clean_str(raw.tobytes().decode('utf-8', errors='replace'))
        elif raw.dtype.type is np.str_:
            return _clean_str(''.join(raw))
        else:
            return _clean_str(str(raw))
    else:
        return _clean_str(str(raw))


# --- Worker pool: the file is opened ONCE per process (not per batch) ---
_worker_ds = None


def _init_worker(file_path):
    global _worker_ds
    _worker_ds = nc.Dataset(file_path, mode='r')


def _process_batch(batch_data):
    """For each block, identify the station and the variable codes present."""
    global _worker_ds
    obs_var = _worker_ds.variables["observed_variable"]
    station_var = _worker_ds.variables["station_name|station_configuration"]
    results = []
    for start_idx, end_idx, date_str in batch_data:
        codes_arr = np.asarray(obs_var[start_idx:end_idx]).ravel()
        unique_codes = sorted(set(int(c) for c in codes_arr))
        station_name = get_station_name(station_var[start_idx])
        results.append({
            'start_idx': start_idx,
            'end_idx': end_idx,
            'station_name': station_name,
            'date_str': date_str,
            'codes': unique_codes,
        })
    return results


def build_catalog_parallel(ds, timestamps, start_indices, end_indices, file_path, progress_cb=None):
    ts_var = ds.variables["report_timestamp"]
    if progress_cb:
        progress_cb(0, 0, "Converting timestamps...")
    dates = nc.num2date(timestamps[start_indices],
                         units=ts_var.units,
                         calendar=getattr(ts_var, 'calendar', 'standard'))
    date_strs = [d.strftime("%Y-%m-%d %H:%M:%S") for d in dates]

    all_launches = list(zip(start_indices, end_indices, date_strs))
    batches = [all_launches[i:i + BATCH_SIZE] for i in range(0, len(all_launches), BATCH_SIZE)]
    total_batches = len(batches)

    results_flat = []
    with MP_CTX.Pool(processes=NUM_WORKERS, initializer=_init_worker, initargs=(file_path,)) as pool:
        for i, res in enumerate(pool.imap(_process_batch, batches)):
            results_flat.extend(res)
            if progress_cb:
                progress_cb(i + 1, total_batches,
                            f"Processed {i + 1}/{total_batches} batches ({len(results_flat)} launches)")

    codes = list(ds.variables["observed_variable"].codes)
    labels = [_clean_str(label) for label in ds.variables["observed_variable"].labels.split(',')]
    code_to_label = {code: labels[i] for i, code in enumerate(codes)}

    catalog = []
    for res in results_flat:
        variable_labels = {c: code_to_label.get(c, f"Code {c}") for c in res['codes']}
        catalog.append({
            'station_name': res['station_name'],
            'launch_date': res['date_str'],
            'start_idx': res['start_idx'],
            'end_idx': res['end_idx'],
            'variable_labels': variable_labels,  # {code: label}
        })
    return catalog


def load_or_build_catalog(file_path, progress_cb=None):
    cache_file = get_cache_path(file_path)
    stat = os.stat(file_path)
    fingerprint = (stat.st_size, stat.st_mtime, MAX_LAUNCHES)

    if os.path.exists(cache_file):
        try:
            with open(cache_file, 'rb') as f:
                cached = pickle.load(f)
            if cached.get('fingerprint') == fingerprint and cached.get('version') == CATALOG_VERSION:
                if progress_cb:
                    progress_cb(1, 1, "Loaded from cache.")
                return cached['catalog']
        except Exception:
            pass

    ds = nc.Dataset(file_path, mode='r')
    try:
        if progress_cb:
            progress_cb(0, 0, "Scanning dataset indices...")
        timestamps = ds.variables["report_timestamp"][:]
        change_points = np.where(timestamps[:-1] != timestamps[1:])[0] + 1
        start_indices = np.insert(change_points, 0, 0)
        end_indices = np.append(change_points, len(timestamps))

        if MAX_LAUNCHES is not None:
            start_indices = start_indices[:MAX_LAUNCHES]
            end_indices = end_indices[:MAX_LAUNCHES]

        catalog = build_catalog_parallel(ds, timestamps, start_indices, end_indices,
                                          file_path, progress_cb=progress_cb)

        all_codes_seen = set()
        for launch in catalog:
            all_codes_seen.update(launch['variable_labels'].items())
        print(f"\n--- Observed variables diagnostics ({os.path.basename(file_path)}) ---")
        if all_codes_seen:
            for code, label in sorted(all_codes_seen):
                print(f"  code {code}: {label}")
        else:
            print("  No variables found in the scanned launches.")
        print("----------------------------------------\n")
    finally:
        ds.close()

    with open(cache_file, 'wb') as f:
        pickle.dump({'fingerprint': fingerprint, 'version': CATALOG_VERSION, 'catalog': catalog}, f)

    return catalog


# ==================================================================
#  APP
# ==================================================================
class GruanViewerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("GRUAN NetCDF Profile Viewer")
        self.root.geometry("1400x850")

        self.ds = None
        self.catalog = []
        self.station_to_dates = defaultdict(set)
        self.launch_key_to_blocks = defaultdict(list)
        self.current_file_path = None
        self.plots_visible = False

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)

        # No file is loaded automatically at startup: the plots stay hidden
        # until the user opens a NetCDF file via "Open file...".
        self.file_label.config(text="No file open — use 'Open file...'")

    # ---------- UI construction (done once) ----------
    def _build_ui(self):
        # --- Top bar: file opening ---
        top_bar = ttk.Frame(self.root, padding=(8, 8, 8, 0))
        top_bar.pack(side=tk.TOP, fill=tk.X)

        ttk.Button(top_bar, text="Open file...", command=self._on_open_file_clicked).pack(side=tk.LEFT)
        self.file_label = ttk.Label(top_bar, text="No file open", font=("Arial", 10, "italic"))
        self.file_label.pack(side=tk.LEFT, padx=12)

        self.load_progress = ttk.Progressbar(top_bar, mode="indeterminate", length=200)
        self.load_status_label = ttk.Label(top_bar, text="")
        # (packed/unpacked dynamically only while loading)

        # --- Controls bar: station / date / variable ---
        control_frame = ttk.Frame(self.root, padding=8)
        control_frame.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(control_frame, text="Station:").pack(side=tk.LEFT, padx=(0, 5))
        self.station_combo = ttk.Combobox(control_frame, width=30, state="disabled")
        self.station_combo.pack(side=tk.LEFT, padx=(0, 15))
        self.station_combo.bind("<<ComboboxSelected>>", self._on_station_change)

        ttk.Label(control_frame, text="Launch date:").pack(side=tk.LEFT, padx=5)
        self.date_combo = ttk.Combobox(control_frame, width=22, state="disabled")
        self.date_combo.pack(side=tk.LEFT, padx=(0, 15))
        self.date_combo.bind("<<ComboboxSelected>>", self._on_date_change)

        ttk.Label(control_frame, text="Variable:").pack(side=tk.LEFT, padx=5)
        self.var_combo = ttk.Combobox(control_frame, width=30, state="disabled")
        self.var_combo.pack(side=tk.LEFT, padx=(0, 15))
        self.var_combo.bind("<<ComboboxSelected>>", lambda e: self._update_plots())

        # --- Two side-by-side panels, each with a figure + toolbar ---
        # Not packed yet: the plot area stays hidden until a file is opened.
        self.plots_frame = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)

        left_frame = ttk.Frame(self.plots_frame)
        right_frame = ttk.Frame(self.plots_frame)
        self.plots_frame.add(left_frame, weight=1)
        self.plots_frame.add(right_frame, weight=1)

        self.fig_val, self.ax_val = plt.subplots(figsize=(6, 6))
        self.canvas_val = FigureCanvasTkAgg(self.fig_val, master=left_frame)
        toolbar_val = NavigationToolbar2Tk(self.canvas_val, left_frame, pack_toolbar=False)
        toolbar_val.update()
        toolbar_val.pack(side=tk.BOTTOM, fill=tk.X)
        self.canvas_val.get_tk_widget().pack(side=tk.BOTTOM, fill=tk.BOTH, expand=True)

        self.fig_unc, self.ax_unc = plt.subplots(figsize=(6, 6))
        self.canvas_unc = FigureCanvasTkAgg(self.fig_unc, master=right_frame)
        toolbar_unc = NavigationToolbar2Tk(self.canvas_unc, right_frame, pack_toolbar=False)
        toolbar_unc.update()
        toolbar_unc.pack(side=tk.BOTTOM, fill=tk.X)
        self.canvas_unc.get_tk_widget().pack(side=tk.BOTTOM, fill=tk.BOTH, expand=True)

        self.status_bar = ttk.Label(self.root, text="", anchor="w", relief=tk.SUNKEN)
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)

    def _show_plots(self):
        """Makes the plot area visible (called once a file has been opened)."""
        if not self.plots_visible:
            self.plots_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
            self.plots_visible = True

    def _hide_plots(self):
        """Hides the plot area (used when no file is open)."""
        if self.plots_visible:
            self.plots_frame.pack_forget()
            self.plots_visible = False

    # ---------- Opening a file ----------
    def _on_open_file_clicked(self):
        path = filedialog.askopenfilename(
            title="Select a NetCDF file",
            filetypes=[("NetCDF files", "*.nc"), ("All files", "*.*")]
        )
        if path:
            self._open_file(path)

    def _open_file(self, file_path):
        # Reset the UI while we load the new file
        self.station_combo.set('')
        self.station_combo['values'] = []
        self.station_combo['state'] = 'disabled'
        self.date_combo.set('')
        self.date_combo['values'] = []
        self.date_combo['state'] = 'disabled'
        self.var_combo.set('')
        self.var_combo['values'] = []
        self.var_combo['state'] = 'disabled'
        self.ax_val.clear()
        self.ax_unc.clear()
        self.canvas_val.draw()
        self.canvas_unc.draw()
        self.status_bar.config(text="")

        self.file_label.config(text=f"Loading: {os.path.basename(file_path)}...")
        self.load_progress.pack(side=tk.LEFT, padx=10)
        self.load_status_label.pack(side=tk.LEFT, padx=5)
        self.load_progress.start(10)

        if self.ds is not None:
            try:
                self.ds.close()
            except Exception:
                pass
            self.ds = None

        self.current_file_path = file_path
        self.progress_queue = queue.Queue()
        threading.Thread(target=self._load_worker, args=(file_path,), daemon=True).start()
        self.root.after(100, self._poll_progress)

    def _load_worker(self, file_path):
        def cb(done, total, msg):
            self.progress_queue.put(('progress', done, total, msg))
        try:
            catalog = load_or_build_catalog(file_path, progress_cb=cb)
            self.progress_queue.put(('done', catalog, file_path))
        except Exception as e:
            self.progress_queue.put(('error', str(e)))

    def _poll_progress(self):
        try:
            while True:
                item = self.progress_queue.get_nowait()
                if item[0] == 'progress':
                    _, done, total, msg = item
                    if total > 0:
                        self.load_progress['mode'] = 'determinate'
                        self.load_progress['value'] = 100 * done / total
                    else:
                        self.load_progress['mode'] = 'indeterminate'
                    self.load_status_label.config(text=msg)
                elif item[0] == 'done':
                    self._finish_loading(item[1], item[2])
                    return
                elif item[0] == 'error':
                    self._loading_failed(item[1])
                    return
        except queue.Empty:
            pass
        self.root.after(100, self._poll_progress)

    def _finish_loading(self, catalog, file_path):
        self.load_progress.stop()
        self.load_progress.pack_forget()
        self.load_status_label.pack_forget()

        self.catalog = catalog
        self.current_file_path = file_path
        self.ds = nc.Dataset(file_path, mode='r')
        self.file_label.config(text=f"File: {os.path.basename(file_path)}")

        self.station_to_dates = defaultdict(set)
        self.launch_key_to_blocks = defaultdict(list)
        for i, launch in enumerate(catalog):
            key = (launch['station_name'], launch['launch_date'])
            self.station_to_dates[launch['station_name']].add(launch['launch_date'])
            self.launch_key_to_blocks[key].append(i)

        self.station_combo['values'] = sorted(self.station_to_dates.keys())
        self.station_combo['state'] = 'readonly'

        # The file loaded successfully: make the plot area visible.
        self._show_plots()

        if self.station_combo['values']:
            self.station_combo.current(0)
            self._on_station_change()

    def _loading_failed(self, error_msg):
        self.load_progress.stop()
        self.load_progress.pack_forget()
        self.load_status_label.pack_forget()
        self.file_label.config(text="No file open — use 'Open file...'")
        self._hide_plots()
        messagebox.showerror("Error opening file",
                              f"Could not open the NetCDF file:\n{error_msg}")

    # ---------- Cascade: station -> date -> variable ----------
    def _on_station_change(self, event=None):
        station = self.station_combo.get()
        dates = sorted(self.station_to_dates.get(station, []))
        self.date_combo['values'] = dates
        self.date_combo['state'] = 'readonly' if dates else 'disabled'
        self.var_combo.set('')
        self.var_combo['values'] = []
        self.var_combo['state'] = 'disabled'
        self.ax_val.clear()
        self.ax_unc.clear()
        self.canvas_val.draw()
        self.canvas_unc.draw()
        if dates:
            self.date_combo.current(0)
            self._on_date_change()
        else:
            self.date_combo.set('')

    def _on_date_change(self, event=None):
        station = self.station_combo.get()
        date = self.date_combo.get()
        block_indices = self.launch_key_to_blocks.get((station, date), [])
        if not block_indices:
            return
        self._current_blocks = block_indices

        label_to_block_code = {}
        for block_idx in block_indices:
            block = self.catalog[block_idx]
            for code, lbl in block['variable_labels'].items():
                key = f"{lbl} (code {code})"
                label_to_block_code[key] = (block_idx, code)

        labels = sorted(label_to_block_code.keys())
        self.var_combo['values'] = labels
        self.var_combo['state'] = 'readonly' if labels else 'disabled'
        self._code_by_label = label_to_block_code
        if labels:
            self.var_combo.current(0)
            self._update_plots()

    # ---------- Plot ----------
    def _update_plots(self):
        var_label = self.var_combo.get()
        entry = getattr(self, '_code_by_label', {}).get(var_label)
        if entry is None:
            return
        block_idx, code = entry
        launch = self.catalog[block_idx]
        s_idx, e_idx = launch['start_idx'], launch['end_idx']

        obs_codes = np.asarray(self.ds.variables["observed_variable"][s_idx:e_idx]).ravel()
        mask = obs_codes == code

        z = np.asarray(self.ds.variables["z_coordinate"][s_idx:e_idx])[mask]
        sort_idx = np.argsort(z)
        z_sorted = z[sort_idx]

        # --- Plot of variable values (altitude on Y, value on X) ---
        self.ax_val.clear()
        try:
            values = np.asarray(self.ds.variables[VALUE_VAR_NAME][s_idx:e_idx])[mask]
            self.ax_val.plot(values[sort_idx], z_sorted, marker='o', markersize=3,
                              linewidth=1.2, color='tab:blue')
            self.ax_val.set_xlabel(var_label.split(' (code')[0])
        except KeyError:
            self.ax_val.text(0.5, 0.5, f"Variable '{VALUE_VAR_NAME}' not found in file",
                              ha='center', va='center', transform=self.ax_val.transAxes)
        self.ax_val.set_title(f"Observed values\n{launch['station_name']} | {launch['launch_date']}", fontsize=10)
        self.ax_val.set_ylabel('Z Coordinate (Altitude / Proxy)')
        self.ax_val.grid(True, linestyle='--', alpha=0.6)
        self.fig_val.tight_layout()
        self.canvas_val.draw()

        # --- Plot of uncertainties (altitude on Y, value on X) ---
        u1 = np.asarray(self.ds.variables["uncertainty_value1"][s_idx:e_idx])[mask]
        u2 = np.asarray(self.ds.variables["uncertainty_value2"][s_idx:e_idx])[mask]
        u5 = np.asarray(self.ds.variables["uncertainty_value5"][s_idx:e_idx])[mask]

        self.ax_unc.clear()
        self.ax_unc.plot(u1[sort_idx], z_sorted, label='uncertainty_value1', marker='o', markersize=3, linewidth=1.2)
        self.ax_unc.plot(u2[sort_idx], z_sorted, label='uncertainty_value2', marker='s', markersize=3, linewidth=1.2)
        self.ax_unc.plot(u5[sort_idx], z_sorted, label='uncertainty_value5', marker='^', markersize=3, linewidth=1.2)
        self.ax_unc.set_title(f"Uncertainties\n{launch['station_name']} | {launch['launch_date']}", fontsize=10)
        self.ax_unc.set_ylabel('Z Coordinate (Altitude / Proxy)')
        self.ax_unc.set_xlabel('Uncertainty Value')
        self.ax_unc.grid(True, linestyle='--', alpha=0.6)
        self.ax_unc.legend()
        self.fig_unc.tight_layout()
        self.canvas_unc.draw()

        self.status_bar.config(
            text=f"{launch['station_name']} | {launch['launch_date']} | {var_label} | {int(mask.sum())} points")

    def _on_closing(self):
        if self.ds is not None:
            try:
                self.ds.close()
            except Exception:
                pass
        plt.close(self.fig_val)
        plt.close(self.fig_unc)
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = GruanViewerApp(root)
    root.mainloop()