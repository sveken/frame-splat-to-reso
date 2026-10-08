"""Windows interface for converting Arcturus recordings to splats and textured meshes."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import queue
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from config import ROOT, STATE, OUTPUTS, preferences, save_preferences, lichtfeld_path, runtime_python
from flow import PRESETS, OUTPUT_TYPES, NO_WINDOW, gpu_info, make_job, process_alive, read_json
from mesh import MESH_PRESETS, FUSION_MODES, depth_image_size

BG, CARD, FG, MUTED, ACCENT = '#111822', '#1b2634', '#edf3fa', '#aab9cb', '#6fe3c3'
APP_TITLE = 'Frame Splat & Mesh to Resonite'


def window_work_area(window):
    """Return the current monitor's usable area and the window frame size."""
    class MonitorInfo(ctypes.Structure):
        _fields_ = [('cbSize', wintypes.DWORD), ('rcMonitor', wintypes.RECT),
                    ('rcWork', wintypes.RECT), ('dwFlags', wintypes.DWORD)]

    user32 = ctypes.WinDLL('user32', use_last_error=True)
    signatures = {
        'GetAncestor': ([wintypes.HWND, wintypes.UINT], wintypes.HWND),
        'MonitorFromWindow': ([wintypes.HWND, wintypes.DWORD], wintypes.HANDLE),
        'GetMonitorInfoW': ([wintypes.HANDLE, ctypes.POINTER(MonitorInfo)], wintypes.BOOL),
        'GetWindowRect': ([wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
        'GetClientRect': ([wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
        'SetWindowPos': ([wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                          ctypes.c_int, ctypes.c_int, wintypes.UINT], wintypes.BOOL),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(user32, name)
        function.argtypes, function.restype = arguments, result
    handle = user32.GetAncestor(window.winfo_id(), 2)  # GA_ROOT: include the title bar.
    monitor = user32.MonitorFromWindow(handle, 2)  # MONITOR_DEFAULTTONEAREST.
    info = MonitorInfo(cbSize=ctypes.sizeof(MonitorInfo))
    outer, client = wintypes.RECT(), wintypes.RECT()
    if not (user32.GetMonitorInfoW(monitor, ctypes.byref(info))
            and user32.GetWindowRect(handle, ctypes.byref(outer))
            and user32.GetClientRect(handle, ctypes.byref(client))):
        raise ctypes.WinError(ctypes.get_last_error())
    frame_width = outer.right - outer.left - (client.right - client.left)
    frame_height = outer.bottom - outer.top - (client.bottom - client.top)
    work = info.rcWork
    return (work.left, work.top, work.right, work.bottom), (frame_width, frame_height), user32, handle


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.iconbitmap(str(ROOT / 'app.ico'))
        self.configure(bg=BG)
        self.geometry('980x820')
        self.minsize(760, 610)
        self.protocol('WM_DELETE_WINDOW', self.close)
        STATE.mkdir(parents=True, exist_ok=True)
        saved = preferences()
        self.job = None
        self.log_position = 0
        self.gpu_updates = queue.Queue()
        self.video = tk.StringVar(value=saved.get('video', ''))
        self.output = tk.StringVar(value=saved.get('output', str(OUTPUTS)))
        self.lichtfeld = tk.StringVar(value=str(lichtfeld_path() or ''))
        self.preset = tk.StringVar(value=saved.get('preset', 'Preview'))
        if self.preset.get() not in PRESETS:
            self.preset.set('Preview')
        self.output_type = tk.StringVar(value=saved.get('output_type', 'Splat'))
        if self.output_type.get() not in OUTPUT_TYPES:
            self.output_type.set('Splat')
        self.fusion_choice = tk.StringVar(value=FUSION_MODES.get(saved.get('fusion_mode'), FUSION_MODES['auto']))
        self.prepared = tk.StringVar()
        self.gpu_label = tk.StringVar(value='Checking NVIDIA GPU…')
        self.stage = tk.StringVar(value='Choose a recording to begin')
        self.detail = tk.StringVar()
        self.result_text = tk.StringVar(value='Each conversion saves to a new folder. Your recording stays unchanged.')
        self.setup_style()

        # Scroll on smaller screens instead of hiding the final action buttons.
        shell = ttk.Frame(self)
        shell.pack(fill='both', expand=True)
        self.canvas = tk.Canvas(shell, bg=BG, highlightthickness=0)
        scroll = ttk.Scrollbar(shell, orient='vertical', command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.canvas.pack(side='left', fill='both', expand=True)
        main = ttk.Frame(self.canvas, padding=24)
        content = self.canvas.create_window((0, 0), window=main, anchor='nw')
        main.bind('<Configure>', lambda e: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>', lambda e: self.canvas.itemconfigure(content, width=e.width))
        self.bind_all('<MouseWheel>', self.scroll_page, add='+')
        main.columnconfigure(0, weight=1)
        ttk.Label(main, text=APP_TITLE, style='Title.TLabel').grid(row=0, sticky='w')
        ttk.Label(main, text='Arcturus recording → splat or textured mesh → Resonite', style='Muted.TLabel').grid(row=1, sticky='w', pady=(3, 14))
        hardware = ttk.Frame(main)
        hardware.grid(row=2, sticky='ew', pady=(0, 10))
        ttk.Label(hardware, textvariable=self.gpu_label, style='Muted.TLabel', wraplength=650).pack(side='left')
        ttk.Button(hardware, text='Check GPU', command=self.refresh_gpu).pack(side='right')
        self.file_row(main, 3, 'Recording', self.video, self.pick_video)
        self.file_row(main, 5, 'Save results in', self.output, self.pick_output)
        self.file_row(main, 7, 'LichtFeld Studio (Splat / Both only)', self.lichtfeld, self.pick_lichtfeld)
        options = ttk.Frame(main)
        options.grid(row=9, sticky='ew', pady=(14, 4))
        quality = ttk.Frame(options)
        quality.pack(fill='x')
        ttk.Label(quality, text='Output', style='Section.TLabel').pack(side='left')
        output_combo = ttk.Combobox(quality, values=OUTPUT_TYPES, textvariable=self.output_type,
                                    state='readonly', width=10)
        output_combo.pack(side='left', padx=(10, 30))
        output_combo.bind('<<ComboboxSelected>>', lambda e: self.describe())
        ttk.Label(quality, text='Quality', style='Section.TLabel').pack(side='left')
        combo = ttk.Combobox(quality, values=list(PRESETS), textvariable=self.preset, state='readonly', width=14)
        combo.pack(side='right')
        combo.bind('<<ComboboxSelected>>', lambda e: self.describe())
        fusion_row = ttk.Frame(options)
        fusion_row.pack(fill='x', pady=(8, 0))
        ttk.Label(fusion_row, text='Mesh processing', style='Section.TLabel').pack(side='left')
        self.fusion_combo = ttk.Combobox(fusion_row, values=list(FUSION_MODES.values()),
                                         textvariable=self.fusion_choice, state='readonly', width=28)
        self.fusion_combo.pack(side='left', padx=(10, 0))
        self.fusion_combo.bind('<<ComboboxSelected>>', lambda e: self.describe())
        ttk.Label(main, textvariable=self.detail, style='Muted.TLabel', wraplength=820).grid(row=10, sticky='w', pady=(0, 12))
        actions = ttk.Frame(main)
        actions.grid(row=11, sticky='ew')
        self.start_button = ttk.Button(actions, text='Convert', style='Start.TButton', command=self.start)
        self.start_button.pack(side='left')
        self.stop_button = ttk.Button(actions, text='Stop', command=self.stop, state='disabled')
        self.stop_button.pack(side='left', padx=8)
        ttk.Button(actions, text='Quick guide', command=lambda: os.startfile(str(ROOT / 'Guide.html'))).pack(side='right')
        reuse = ttk.Frame(main)
        reuse.grid(row=12, sticky='ew', pady=(10, 0))
        ttk.Button(reuse, text='Reuse prepared scan…', command=self.pick_prepared).pack(side='left')
        ttk.Button(reuse, text='Clear', command=lambda: self.prepared.set('')).pack(side='left', padx=8)
        ttk.Button(reuse, text='Open previous run…', command=self.load_previous).pack(side='right')
        ttk.Label(main, textvariable=self.prepared, style='Muted.TLabel', wraplength=820).grid(row=13, sticky='w', pady=(4, 0))
        ttk.Separator(main).grid(row=14, sticky='ew', pady=14)
        ttk.Label(main, textvariable=self.stage, style='Section.TLabel', wraplength=820).grid(row=15, sticky='w')
        self.progress = ttk.Progressbar(main, maximum=100)
        self.progress.grid(row=16, sticky='ew', pady=(8, 6))
        ttk.Label(main, textvariable=self.result_text, style='Muted.TLabel', wraplength=820).grid(row=17, sticky='w')
        results = ttk.Frame(main)
        results.grid(row=18, sticky='ew', pady=(10, 4))
        ttk.Button(results, text='Open result folder', command=self.open_folder).pack(side='left')
        self.view_button = ttk.Button(results, text='Inspect / crop in LichtFeld', command=self.view, state='disabled')
        self.view_button.pack(side='left', padx=8)
        self.mesh_button = ttk.Button(results, text='Open mesh files', command=self.open_mesh, state='disabled')
        self.mesh_button.pack(side='left')
        self.details_button = ttk.Button(results, text='Show log', command=self.toggle_log)
        self.details_button.pack(side='right')
        self.log = tk.Text(main, height=9, bg='#0c121b', fg=MUTED, relief='flat', wrap='word',
                           font=('Consolas', 9), state='disabled', padx=10, pady=10)
        self.log.grid(row=19, sticky='ew', pady=8)
        self.log.grid_remove()
        self.log_visible = False
        ttk.Label(main, text='Progress is approximate. Closing this window keeps an active conversion running.',
                  style='Muted.TLabel', wraplength=820).grid(row=20, sticky='w', pady=(8, 0))
        ttk.Label(main, text='Program creation was supervised by Sveken',
                  style='Muted.TLabel', wraplength=820).grid(row=21, sticky='w', pady=(14, 0))
        latest = read_json(STATE / 'last_job.json').get('path')
        self.last_seen_latest = latest
        if latest and (Path(latest) / 'settings.json').is_file():
            self.attach(Path(latest))
        self.describe()
        self.prepared.trace_add('write', lambda *_: self.describe())
        self.fit_startup_window(main, scroll)
        self.refresh_gpu()
        self.after(500, self.poll)

    def fit_startup_window(self, main, scroll):
        self.update_idletasks()
        try:
            (left, top, right, bottom), (frame_width, frame_height), user32, handle = window_work_area(self)
        except (AttributeError, OSError):
            left, top, right, bottom = 0, 0, self.winfo_screenwidth(), self.winfo_screenheight()
            frame_width, frame_height = 32, 80
            user32 = None
        # Account for Windows scaling, title bar, taskbar and a small desktop margin.
        available_width = max(1, right - left - frame_width - 32)
        available_height = max(1, bottom - top - frame_height - 32)
        width = min(max(980, main.winfo_reqwidth() + scroll.winfo_reqwidth()), available_width)
        height = min(main.winfo_reqheight(), available_height)
        self.minsize(min(760, available_width), min(610, available_height))
        self.geometry(f'{width}x{height}')
        self.update_idletasks()
        if user32 is not None:
            x = left + (right - left - width - frame_width) // 2
            y = top + (bottom - top - height - frame_height) // 2
            # Position on this monitor, including monitors with negative desktop coordinates.
            user32.SetWindowPos(handle, None, x, y, 0, 0, 0x0015)  # NOSIZE | NOZORDER | NOACTIVATE.

    def setup_style(self):
        style = ttk.Style(self)
        style.theme_use('clam')
        style.configure('.', background=BG, foreground=FG, font=('Segoe UI', 10))
        style.configure('TFrame', background=BG)
        style.configure('TLabel', background=BG, foreground=FG)
        style.configure('Muted.TLabel', foreground=MUTED)
        style.configure('Title.TLabel', font=('Segoe UI Semibold', 24))
        style.configure('Section.TLabel', font=('Segoe UI Semibold', 11))
        style.configure('TEntry', fieldbackground=CARD, foreground=FG, insertcolor=FG, padding=7)
        style.configure('TCombobox', fieldbackground=CARD, background=CARD, foreground=FG, padding=6)
        style.map('TCombobox', fieldbackground=[('readonly', CARD)], foreground=[('readonly', FG)])
        style.configure('TButton', background=CARD, foreground=FG, padding=(12, 7), borderwidth=0)
        style.map('TButton', background=[('active', '#34465c')], foreground=[('disabled', '#718298')])
        style.configure('Start.TButton', background=ACCENT, foreground=BG, font=('Segoe UI Semibold', 11))
        style.map('Start.TButton', background=[('active', '#a5f2de'), ('disabled', '#334e4b')])
        style.configure('TProgressbar', background=ACCENT, troughcolor=CARD, borderwidth=0)
        self.option_add('*TCombobox*Listbox.background', CARD)
        self.option_add('*TCombobox*Listbox.foreground', FG)

    def scroll_page(self, event):
        if event.widget is not self.log:
            self.canvas.yview_scroll(-int(event.delta / 120), 'units')

    def file_row(self, parent, row, label, variable, callback):
        ttk.Label(parent, text=label, style='Section.TLabel').grid(row=row, sticky='w', pady=(6, 4))
        frame = ttk.Frame(parent)
        frame.grid(row=row + 1, sticky='ew')
        frame.columnconfigure(0, weight=1)
        ttk.Entry(frame, textvariable=variable).grid(row=0, column=0, sticky='ew')
        ttk.Button(frame, text='Browse…', command=callback).grid(row=0, column=1, padx=(8, 0))

    def describe(self):
        self.fusion_combo.configure(state='readonly' if self.output_type.get() in ('Mesh', 'Both') else 'disabled')
        name = self.preset.get()
        p = PRESETS[name]
        hint = {'Preview': 'Start here to check a new recording.',
                'Detailed': 'More detail for objects and short scans.',
                'Room': 'More viewpoints for a room or connected area.'}[name]
        resolution = 'full resolution' if p['resize'] == 1 else 'half resolution'
        capture = 'Existing images and camera positions will be reused.' if self.prepared.get() else f'{p["frames"]} stereo pairs'
        lines = [f'{hint} {capture}']
        if self.output_type.get() in ('Splat', 'Both'):
            lines.append(f'Splat: {resolution} · {p["iterations"]:,} steps · up to {p["cap"]:,} splats')
        if self.output_type.get() in ('Mesh', 'Both'):
            m = MESH_PRESETS[name]
            lines.append(f'Mesh: up to {depth_image_size(m, self.fusion_mode()):,} px depth · target {m["target_faces"]:,} triangles · '
                         f'up to {m["texture_size"]:,} px per texture · OBJ + textures')
            if self.fusion_mode() == 'auto':
                lines.append('Fits surface fusion in RAM for speed; photo texture quality stays the same.')
            elif self.fusion_mode() == 'balanced':
                lines.append('Aims for 1,024 px surface fusion using more RAM; lowers it if needed. Texture quality stays the same.')
            else:
                lines.append('Keeps full surface detail. Large rooms can take hours if they exceed available RAM.')
        self.detail.set('\n'.join(lines))

    def fusion_mode(self):
        return next(key for key, label in FUSION_MODES.items() if label == self.fusion_choice.get())

    def refresh_gpu(self):
        threading.Thread(target=lambda: self.gpu_updates.put(gpu_info()), daemon=True).start()

    def pick_video(self):
        value = filedialog.askopenfilename(title='Choose the original synced Arcturus MP4',
                    initialdir=Path(self.video.get()).parent if self.video.get() else Path.home() / 'Videos',
                    filetypes=[('MP4 recording', '*.mp4')])
        if value:
            self.video.set(value)
            self.prepared.set('')

    def pick_output(self):
        value = filedialog.askdirectory(title='Save results in', initialdir=self.output.get())
        if value:
            self.output.set(value)

    def pick_lichtfeld(self):
        value = filedialog.askopenfilename(title='Choose LichtFeld-Studio.exe', filetypes=[('Windows application', '*.exe')])
        if value:
            self.lichtfeld.set(value)
            save_preferences(lichtfeld=value)

    def pick_prepared(self):
        value = filedialog.askdirectory(title='Choose a previous run with a completed scan', initialdir=self.output.get())
        if value:
            self.prepared.set(value)

    def save_choices(self):
        save_preferences(lichtfeld=self.lichtfeld.get().strip(), output=self.output.get(),
                         video=self.video.get(), preset=self.preset.get(), output_type=self.output_type.get(),
                         fusion_mode=self.fusion_mode())

    def attach(self, job):
        self.job = Path(job)
        self.log_position = 0
        self.log.configure(state='normal')
        self.log.delete('1.0', 'end')
        self.log.configure(state='disabled')
        settings = read_json(self.job / 'settings.json')
        self.video.set(settings.get('recording', self.video.get()))
        preset = settings.get('preset', 'Preview')
        self.preset.set(preset if preset in PRESETS else 'Preview')
        output_type = settings.get('output_type', 'Splat')
        self.output_type.set(output_type if output_type in OUTPUT_TYPES else 'Splat')
        plan = read_json(self.job / 'fusion-plan.json')
        self.fusion_choice.set(FUSION_MODES.get(plan.get('mode') or settings.get('mesh', {}).get('fusion_mode'),
                                               FUSION_MODES['auto']))
        self.describe()

    def start(self):
        try:
            current = read_json(STATE / 'running.lock')
            if process_alive(current.get('pid')):
                self.attach(Path(current['path']))
                return
            if not gpu_info()['available']:
                raise RuntimeError('No NVIDIA CUDA GPU is available. Enable your NVIDIA GPU and check its driver, then try again.')
            self.save_choices()
            job = make_job(self.video.get(), self.output.get(), self.preset.get(),
                           self.prepared.get() or None, lichtfeld=self.lichtfeld.get().strip(),
                           output_type=self.output_type.get(), fusion_mode=self.fusion_mode())
            subprocess.Popen([runtime_python(), '-u', str(ROOT / 'flow.py'), '--job', str(job)],
                             cwd=ROOT, creationflags=NO_WINDOW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.prepared.set('')
            self.attach(job)
            self.start_button.configure(state='disabled')
        except Exception as error:
            messagebox.showerror('Cannot start conversion', str(error))

    def stop(self):
        if self.job:
            (self.job / 'STOP').touch()
            self.stage.set('Stopping… Existing files will be kept.')

    def poll(self):
        try:
            while True:
                gpu = self.gpu_updates.get_nowait()
                self.gpu_label.set(f'{gpu["name"]} · {gpu.get("memory_gb", 0):g} GB VRAM' if gpu['available']
                                   else 'NVIDIA GPU unavailable. Check that it is enabled and its driver is installed.')
        except queue.Empty:
            pass
        latest = read_json(STATE / 'last_job.json').get('path')
        if latest and latest != self.last_seen_latest:
            self.last_seen_latest = latest
            if (Path(latest) / 'settings.json').is_file():
                self.attach(Path(latest))
        if self.job:
            status = read_json(self.job / 'status.json')
            running = status.get('status') in ('running', 'queued')
            if status.get('status') == 'running' and not process_alive(status.get('pid')):
                running = False
                status = dict(status, stage='Conversion interrupted', error='Files were kept. Start a new run; reuse the scan if reconstruction finished.')
            done = status.get('status') == 'complete'
            self.stage.set('Conversion complete — inspect before import' if done else status.get('stage', 'Loading…'))
            self.progress['value'] = status.get('progress', 0)
            self.start_button.configure(state='disabled' if running else 'normal')
            self.stop_button.configure(state='normal' if running else 'disabled')
            final = self.job / 'scene.ply'
            result = status.get('result', {})
            artifacts = result.get('artifacts', {'splat': result} if result.get('gaussians') else {})
            self.view_button.configure(state='normal' if artifacts.get('splat') and final.is_file() else 'disabled')
            self.mesh_button.configure(state='normal' if artifacts.get('mesh') and (self.job / 'mesh/scene.obj').is_file() else 'disabled')
            if status.get('error'):
                self.result_text.set(status['error'])
            elif done:
                descriptions = []
                if artifacts.get('splat'):
                    descriptions.append(f'{artifacts["splat"]["gaussians"]:,} splats. Inspect / crop in LichtFeld before import.')
                if artifacts.get('mesh'):
                    descriptions.append(f'{artifacts["mesh"]["triangles"]:,} triangles. Import mesh/scene.obj as a 3D model; '
                                        'keep its MTL and texture images together. Try Unlit material.')
                self.result_text.set('\n'.join(descriptions))
            else:
                self.result_text.set(f'Run: {self.job.name}')
            try:
                with (self.job / 'conversion.log').open('r', encoding='utf-8', errors='replace') as stream:
                    stream.seek(self.log_position)
                    text = stream.read(60000)
                    self.log_position = stream.tell()
                if text:
                    import re
                    self.log.configure(state='normal')
                    self.log.insert('end', re.sub(r'\x1b\[[0-9;]*m', '', text))
                    if int(self.log.index('end-1c').split('.')[0]) > 350:
                        self.log.delete('1.0', '150.0')
                    self.log.see('end')
                    self.log.configure(state='disabled')
            except OSError:
                pass
        self.after(1000, self.poll)

    def toggle_log(self):
        self.log_visible = not self.log_visible
        if self.log_visible:
            self.log.grid()
        else:
            self.log.grid_remove()
        self.details_button.configure(text='Hide log' if self.log_visible else 'Show log')

    def open_folder(self):
        path = self.job or Path(self.output.get())
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(str(path))

    def view(self):
        try:
            executable = Path(self.lichtfeld.get().strip())
            if not executable.is_file():
                raise FileNotFoundError('Choose LichtFeld-Studio.exe first.')
            if self.job and (self.job / 'scene.ply').is_file():
                subprocess.Popen([str(executable), '--view', str(self.job / 'scene.ply'), '--no-interop'], cwd=executable.parent)
        except OSError as error:
            messagebox.showerror('Cannot open LichtFeld', str(error))

    def open_mesh(self):
        if self.job and (self.job / 'mesh/scene.obj').is_file():
            os.startfile(str(self.job / 'mesh'))

    def load_previous(self):
        value = filedialog.askdirectory(title='Choose a run folder', initialdir=self.output.get())
        if value:
            if not (Path(value) / 'settings.json').is_file():
                messagebox.showerror('Not a run folder', 'Choose the dated folder containing settings.json.')
            else:
                self.attach(Path(value))

    def close(self):
        self.save_choices()
        self.destroy()


if __name__ == '__main__':
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    try:
        App().mainloop()
    except Exception:
        import traceback
        STATE.mkdir(parents=True, exist_ok=True)
        (STATE / 'launcher-error.log').write_text(traceback.format_exc(), encoding='utf-8')
        messagebox.showerror('Could not open Frame Splat', f'See {STATE / "launcher-error.log"} for details.')
        raise
