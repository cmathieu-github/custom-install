#!/usr/bin/env python3

# This file is a part of custom-install.py.
#
# custom-install is copyright (c) 2019 Ian Burgwin
# This file is licensed under The MIT License (MIT).
# You can find the full license text in LICENSE.md in the root of this project.

import shutil
import time
from os import environ, makedirs, scandir, walk
from os.path import abspath, basename, dirname, getsize, isdir, join, isfile, relpath
import sys
from threading import Thread, Lock
from time import strftime
from traceback import format_exception
import tkinter as tk
import tkinter.ttk as ttk
import tkinter.filedialog as fd
import tkinter.messagebox as mb
from typing import TYPE_CHECKING

from pyctr.crypto import MissingSeedError, CryptoEngine, load_seeddb
from pyctr.crypto.engine import b9_paths
from pyctr.util import config_dirs
from pyctr.type.cdn import CDNError
from pyctr.type.cia import CIAError
from pyctr.type.tmd import TitleMetadataError

from . import __version__
from .__main__ import CustomInstall, load_cifinish, InvalidCIFinishError, InstallStatus, save3ds_fuse_path
from .config import load_config, save_config, load_profiles, save_profiles
from .ndscopy import NDSCopier
from . import sdformat

if TYPE_CHECKING:
    from os import PathLike
    from typing import Dict, List, Union

frozen = getattr(sys, 'frozen', None)
is_windows = sys.platform == 'win32'
taskbar = None
if is_windows:
    if frozen:
        # attempt to fix loading tcl/tk when running from a path with non-latin characters
        tkinter_path = dirname(tk.__file__)
        tcl_path = join(tkinter_path, 'tcl8.6')
        environ['TCL_LIBRARY'] = 'lib/tkinter/tcl8.6'
    try:
        import comtypes.client as cc

        tbl = cc.GetModule('TaskbarLib.tlb')

        taskbar = cc.CreateObject('{56FDF344-FD6D-11D0-958A-006097C9A090}', interface=tbl.ITaskbarList3)
        taskbar.HrInit()
    except (ModuleNotFoundError, UnicodeEncodeError, AttributeError):
        pass

file_parent = dirname(abspath(__file__))

# automatically load boot9 if it's in the current directory
b9_paths.insert(0, join(file_parent, 'boot9.bin'))
b9_paths.insert(0, join(file_parent, 'boot9_prot.bin'))

seeddb_paths = [join(x, 'seeddb.bin') for x in config_dirs]
try:
    seeddb_paths.insert(0, environ['SEEDDB_PATH'])
except KeyError:
    pass
# automatically load seeddb if it's in the current directory
seeddb_paths.insert(0, join(file_parent, 'seeddb.bin'))


def clamp(n, smallest, largest):
    return max(smallest, min(n, largest))


def find_first_file(paths):
    for p in paths:
        if isfile(p):
            return p


# find boot9, seeddb, and movable.sed to auto-select in the gui
default_b9_path = find_first_file(b9_paths)
default_seeddb_path = find_first_file(seeddb_paths)
default_movable_sed_path = find_first_file([join(file_parent, 'movable.sed')])

if default_seeddb_path:
    load_seeddb(default_seeddb_path)

statuses = {
    InstallStatus.Waiting: 'Waiting',
    InstallStatus.Starting: 'Starting',
    InstallStatus.Writing: 'Writing',
    InstallStatus.Finishing: 'Finishing',
    InstallStatus.Done: 'Done',
    InstallStatus.Failed: 'Failed',
}


_DRIVE_NONE = '(Aucun)'
DRIVE_LIMIT_EVENT = '<<DriveLimitChanged>>'  # émis quand la limite de taille est modifiée


def _drive_to_path(value):
    """Convert a drive combo value like 'E: — SanDisk (32 Go)' or 'E:' to 'E:\\'."""
    value = (value or '').strip()
    if not value or value == _DRIVE_NONE:
        return ''
    if is_windows and len(value) >= 2 and value[1] == ':':
        rest = value[2:]
        if not rest or rest[0] in (' ', '—', '—'):
            return value[0].upper() + ':\\'
    return value


def _sd_drive_combo_values(config):
    """[_DRIVE_NONE] + lecteurs proposés : tous sauf C:, D: et ceux au-delà de la limite de taille."""
    max_bytes = sdformat.max_bytes_from_config(config)
    return [_DRIVE_NONE] + [d.display() for d in sdformat.list_drives(max_bytes)]


def _refresh_sd_combo_async(widget, combo, config, on_done=None):
    """Recharge la liste des lecteurs dans un thread puis met à jour le combo (garde la sélection)."""
    def work():
        try:
            values = _sd_drive_combo_values(config)
        except Exception:
            values = [_DRIVE_NONE]

        def apply():
            cur_letter = _drive_to_path(combo.get())[:1]
            combo.configure(values=values)
            idx = 0
            for i, v in enumerate(values):
                if cur_letter and _drive_to_path(v)[:1] == cur_letter:
                    idx = i
            combo.current(idx)
            if on_done:
                on_done()
        try:
            widget.after(0, apply)
        except RuntimeError:
            pass  # fenêtre fermée entre-temps
    Thread(target=work, daemon=True).start()


def _setup_drive_combo(widget, combo, config, on_done=None):
    """Remplit le combo au démarrage et à chaque changement de la limite de taille."""
    refresh = lambda *_: _refresh_sd_combo_async(widget, combo, config, on_done)
    # Différé : le thread ne doit rappeler l'interface qu'une fois la boucle Tk démarrée
    widget.after(200, refresh)
    widget.winfo_toplevel().bind(DRIVE_LIMIT_EVENT, refresh, add='+')
    return refresh


def _try_get_id0(movable_path):
    """Extraire l'ID0 depuis un fichier movable.sed (via CryptoEngine).
    Retourne la chaîne hex de l'ID0, ou None en cas d'échec."""
    try:
        crypto = CryptoEngine()
        crypto.setup_sd_key_from_file(movable_path)
        return crypto.id0.hex()
    except Exception:
        return None


class ConsoleFrame(ttk.Frame):
    def __init__(self, parent: tk.BaseWidget = None, starting_lines: 'List[str]' = None):
        super().__init__(parent)
        self.parent = parent

        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        scrollbar = ttk.Scrollbar(self, orient=tk.VERTICAL)
        scrollbar.grid(row=0, column=1, sticky=tk.NSEW)

        self.text = tk.Text(self, highlightthickness=0, wrap='word', yscrollcommand=scrollbar.set)
        self.text.grid(row=0, column=0, sticky=tk.NSEW)

        scrollbar.config(command=self.text.yview)

        if starting_lines:
            for l in starting_lines:
                self.text.insert(tk.END, l + '\n')

        self.text.see(tk.END)
        self.text.configure(state=tk.DISABLED)

    def log(self, *message, end='\n', sep=' '):
        self.text.configure(state=tk.NORMAL)
        self.text.insert(tk.END, sep.join(message) + end)
        self.text.see(tk.END)
        self.text.configure(state=tk.DISABLED)


def simple_listbox_frame(parent, title: 'str', items: 'List[str]'):
    frame = ttk.LabelFrame(parent, text=title)
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)

    scrollbar = ttk.Scrollbar(frame, orient=tk.VERTICAL)
    scrollbar.grid(row=0, column=1, sticky=tk.NSEW)

    box = tk.Listbox(frame, highlightthickness=0, yscrollcommand=scrollbar.set, selectmode=tk.EXTENDED)
    box.grid(row=0, column=0, sticky=tk.NSEW)
    scrollbar.config(command=box.yview)

    box.insert(tk.END, *items)

    box.config(height=clamp(len(items), 3, 10))

    return frame


class TitleReadFailResults(tk.Toplevel):
    def __init__(self, parent: tk.Tk = None, *, failed: 'Dict[str, str]'):
        super().__init__(parent)
        self.parent = parent

        self.wm_withdraw()
        self.wm_transient(self.parent)
        self.grab_set()
        self.wm_title('Failed to add titles')

        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        outer_container = ttk.Frame(self)
        outer_container.grid(sticky=tk.NSEW)
        outer_container.rowconfigure(0, weight=0)
        outer_container.rowconfigure(1, weight=1)
        outer_container.columnconfigure(0, weight=1)

        message_label = ttk.Label(outer_container, text="Some titles couldn't be added.")
        message_label.grid(row=0, column=0, sticky=tk.NSEW, padx=10, pady=10)

        treeview_frame = ttk.Frame(outer_container)
        treeview_frame.grid(row=1, column=0, sticky=tk.NSEW)
        treeview_frame.rowconfigure(0, weight=1)
        treeview_frame.columnconfigure(0, weight=1)

        treeview_scrollbar = ttk.Scrollbar(treeview_frame, orient=tk.VERTICAL)
        treeview_scrollbar.grid(row=0, column=1, sticky=tk.NSEW)

        treeview = ttk.Treeview(treeview_frame, yscrollcommand=treeview_scrollbar.set)
        treeview.grid(row=0, column=0, sticky=tk.NSEW, padx=10, pady=(0, 10))
        treeview.configure(columns=('filepath', 'reason'), show='headings')

        treeview.column('filepath', width=200, anchor=tk.W)
        treeview.heading('filepath', text='File path')
        treeview.column('reason', width=400, anchor=tk.W)
        treeview.heading('reason', text='Reason')

        treeview_scrollbar.configure(command=treeview.yview)

        for path, reason in failed.items():
            treeview.insert('', tk.END, text=path, iid=path, values=(basename(path), reason))

        ok_frame = ttk.Frame(outer_container)
        ok_frame.grid(row=2, column=0, sticky=tk.NSEW, padx=10, pady=(0, 10))
        ok_frame.rowconfigure(0, weight=1)
        ok_frame.columnconfigure(0, weight=1)

        ok_button = ttk.Button(ok_frame, text='OK', command=self.destroy)
        ok_button.grid(row=0, column=0)

        self.wm_deiconify()


class InstallResults(tk.Toplevel):
    def __init__(self, parent: tk.Tk = None, *, install_state: 'Dict[str, List[str]]', copied_3dsx: bool,
                 application_count: int):
        super().__init__(parent)
        self.parent = parent

        self.wm_withdraw()
        self.wm_transient(self.parent)
        self.grab_set()
        self.wm_title('Install results')

        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        outer_container = ttk.Frame(self)
        outer_container.grid(sticky=tk.NSEW)
        outer_container.rowconfigure(0, weight=0)
        outer_container.columnconfigure(0, weight=1)

        if install_state['failed'] and install_state['installed']:
            # some failed and some worked
            message = ('Some titles were installed, some failed. Please check the output for more details.\n'
                       'The ones that were installed can be finished with custom-install-finalize.')
        elif install_state['failed'] and not install_state['installed']:
            # all failed
            message = 'All titles failed to install. Please check the output for more details.'
        elif install_state['installed'] and not install_state['failed']:
            # all worked
            message = 'All titles were installed.'
        else:
            message = 'Nothing was installed.'

        if install_state['installed']:
            if copied_3dsx:
                message += '\n\ncustom-install-finalize has been copied to the SD card.'
            else:
                message += ('\n\nNote: custom-install-finalize was not copied.\n'
                            'You can either manually copy the 3dsx to your SD card, or use GodMode9 to finish the install.')

        if application_count >= 300:
            message += (f'\n\nWarning: {application_count} installed applications were detected.\n'
                        f'The HOME Menu will only show 300 icons.\n'
                        f'Some applications (not updates or DLC) will need to be deleted.')

        message_label = ttk.Label(outer_container, text=message)
        message_label.grid(row=0, column=0, sticky=tk.NSEW, padx=10, pady=10)

        if install_state['installed']:
            outer_container.rowconfigure(1, weight=1)
            frame = simple_listbox_frame(outer_container, 'Installed', install_state['installed'])
            frame.grid(row=1, column=0, sticky=tk.NSEW, padx=10, pady=(0, 10))

        if install_state['failed']:
            outer_container.rowconfigure(2, weight=1)
            frame = simple_listbox_frame(outer_container, 'Failed', install_state['failed'])
            frame.grid(row=2, column=0, sticky=tk.NSEW, padx=10, pady=(0, 10))

        ok_frame = ttk.Frame(outer_container)
        ok_frame.grid(row=3, column=0, sticky=tk.NSEW, padx=10, pady=(0, 10))
        ok_frame.rowconfigure(0, weight=1)
        ok_frame.columnconfigure(0, weight=1)

        ok_button = ttk.Button(ok_frame, text='OK', command=self.destroy)
        ok_button.grid(row=0, column=0)

        self.wm_deiconify()


class CustomInstallGUI(ttk.Frame):
    console = None
    b9_loaded = False

    def __init__(self, parent: tk.Tk = None, config: dict = None):
        super().__init__(parent)
        self.parent = parent
        self.config = config or {}

        self.readers = {}
        self.lock = Lock()
        self.log_messages = []
        self.hwnd = None

        self.rowconfigure(1, weight=1)
        self.columnconfigure(0, weight=1)

        if taskbar:
            def setup_tab():
                self.hwnd = int(self.winfo_toplevel().wm_frame(), 16)
                taskbar.ActivateTab(self.hwnd)
            self.after(100, setup_tab)

        # ---------------------------------------------------------------- #
        # Destination et fichiers requis
        dest_frame = ttk.LabelFrame(self, text='Destination et fichiers requis')
        dest_frame.grid(row=0, column=0, sticky=tk.EW, padx=10, pady=(10, 5))
        dest_frame.columnconfigure(1, weight=1)

        self.file_picker_textboxes = {}

        def sd_callback():
            f = fd.askdirectory(parent=parent,
                                title='Sélectionner la racine SD (le dossier ou lecteur contenant "Nintendo 3DS")',
                                initialdir=file_parent, mustexist=True)
            if f:
                cifinish_path = join(f, 'cifinish.bin')
                try:
                    load_cifinish(cifinish_path)
                except InvalidCIFinishError:
                    self.show_error(f'{cifinish_path} was corrupt!\n\n'
                                    f'This could mean an issue with the SD card or the filesystem. Please check it for errors.\n'
                                    f'It is also possible, though less likely, to be an issue with custom-install.\n\n'
                                    f'Stopping now to prevent possible issues. If you want to try again, delete cifinish.bin from the SD card and re-run custom-install.')
                    return

                sd_selected.delete('1.0', tk.END)
                sd_selected.insert(tk.END, f)

                for filename in ['boot9.bin', 'seeddb.bin', 'movable.sed']:
                    path = auto_input_filename(self, f, filename)
                    if filename == 'boot9.bin':
                        self.check_b9_loaded()
                        self.enable_buttons()
                    if filename == 'seeddb.bin':
                        load_seeddb(path)

        ttk.Label(dest_frame, text='Racine SD :').grid(row=0, column=0, sticky=tk.W, padx=(10, 5), pady=(8, 3))

        sd_inner = ttk.Frame(dest_frame)
        sd_inner.grid(row=0, column=1, sticky=tk.EW, padx=5, pady=(8, 3))
        sd_inner.columnconfigure(2, weight=1)

        self._sd_drive_combo = ttk.Combobox(sd_inner, values=[_DRIVE_NONE], state='readonly', width=30)
        self._sd_drive_combo.current(0)  # (Aucun) par défaut
        self._sd_drive_combo.grid(row=0, column=0, padx=(0, 3))
        refresh_sd_drives = _setup_drive_combo(self, self._sd_drive_combo, self.config)

        ttk.Button(sd_inner, text='↻', width=3, command=refresh_sd_drives).grid(row=0, column=1, padx=(0, 6))

        sd_selected = tk.Text(sd_inner, wrap='none', height=1)
        sd_selected.grid(row=0, column=2, sticky=tk.EW)

        def on_sd_drive_selected(event):
            path = _drive_to_path(self._sd_drive_combo.get())
            sd_selected.delete('1.0', tk.END)
            if not path:
                return
            sd_selected.insert(tk.END, path)
            for filename in ['boot9.bin', 'seeddb.bin', 'movable.sed']:
                p = auto_input_filename(self, path, filename)
                if filename == 'boot9.bin':
                    self.check_b9_loaded()
                    self.enable_buttons()
                if filename == 'seeddb.bin' and p:
                    load_seeddb(p)

        self._sd_drive_combo.bind('<<ComboboxSelected>>', on_sd_drive_selected)

        ttk.Button(dest_frame, text='...', command=sd_callback).grid(row=0, column=2, padx=(0, 3), pady=(8, 3))

        def clear_sd():
            sd_selected.delete('1.0', tk.END)
            self._sd_drive_combo.current(0)  # retour sur (Aucun)
            refresh_sd_drives()

        ttk.Button(dest_frame, text='×', width=2, command=clear_sd).grid(row=0, column=3, padx=(0, 10), pady=(8, 3))
        self.file_picker_textboxes['sd'] = sd_selected

        def auto_input_filename(self, f, filename):
            sd_msed_path = find_first_file(
                [join(f, "gm9", "out", filename), join(f, "boot9strap", filename), join(f, filename)]
            )
            if sd_msed_path:
                self.log('Trouvé ' + filename + ' sur la SD : ' + sd_msed_path)
                if filename.endswith('bin'):
                    filename = filename.split('.')[0]
                box = self.file_picker_textboxes[filename]
                box.delete('1.0', tk.END)
                box.insert(tk.END, sd_msed_path)
                if filename == 'movable.sed':
                    self._update_id0(sd_msed_path)
                return sd_msed_path

        def create_required_file_picker(type_name, types, default, row, callback=lambda filename: None):
            def internal_callback():
                f = fd.askopenfilename(parent=parent, title='Sélectionner ' + type_name,
                                       filetypes=types, initialdir=file_parent)
                if f:
                    selected.delete('1.0', tk.END)
                    selected.insert(tk.END, f)
                    callback(f)

            ttk.Label(dest_frame, text=type_name + ' :').grid(row=row, column=0, sticky=tk.W, padx=(10, 5), pady=3)
            selected = tk.Text(dest_frame, wrap='none', height=1)
            selected.grid(row=row, column=1, sticky=tk.EW, padx=5, pady=3)
            if default:
                selected.insert(tk.END, default)
            ttk.Button(dest_frame, text='...', command=internal_callback).grid(row=row, column=2, padx=(0, 3), pady=3)
            ttk.Button(dest_frame, text='×', width=2,
                       command=lambda s=selected: s.delete('1.0', tk.END)).grid(row=row, column=3, padx=(0, 10), pady=3)
            self.file_picker_textboxes[type_name] = selected

        def b9_callback(path):
            self.check_b9_loaded()
            self.enable_buttons()

        def seeddb_callback(path):
            load_seeddb(path)

        create_required_file_picker('boot9', [('boot9 file', '*.bin')], default_b9_path, 1, b9_callback)
        create_required_file_picker('seeddb', [('seeddb file', '*.bin')], default_seeddb_path, 2, seeddb_callback)
        def movable_callback(path):
            self._update_id0(path)
        create_required_file_picker('movable.sed', [('movable.sed file', '*.sed')], default_movable_sed_path, 3,
                                    movable_callback)

        # ID0 détecté depuis movable.sed
        ttk.Label(dest_frame, text='ID0 :').grid(row=4, column=0, sticky=tk.W, padx=(10, 5), pady=3)
        self._id0_label = ttk.Label(dest_frame, text='(sélectionner movable.sed)', foreground='grey')
        self._id0_label.grid(row=4, column=1, sticky=tk.W, padx=5, pady=3)

        if default_movable_sed_path:
            self._update_id0(default_movable_sed_path)

        # ---------------------------------------------------------------- #
        # Fichiers CIA à installer
        cia_frame = ttk.LabelFrame(self, text='Fichiers CIA à installer')
        cia_frame.grid(row=1, column=0, sticky=tk.NSEW, padx=10, pady=5)
        cia_frame.rowconfigure(2, weight=1)
        cia_frame.columnconfigure(0, weight=1)

        btn_row = ttk.Frame(cia_frame)
        btn_row.grid(row=0, column=0, sticky=tk.EW, padx=5, pady=(5, 2))

        def add_cias_callback():
            files = fd.askopenfilenames(parent=parent, title='Sélectionner des fichiers CIA',
                                        filetypes=[('CIA files', '*.cia')], initialdir=file_parent)
            results = {}
            for f in files:
                success, reason = self.add_cia(f)
                if not success:
                    results[f] = reason
            if results:
                TitleReadFailResults(self.parent, failed=results).focus()
            self.sort_treeview()

        add_cias = ttk.Button(btn_row, text='Ajouter CIA', command=add_cias_callback)
        add_cias.grid(row=0, column=0, padx=(0, 3))

        def add_cdn_callback():
            d = fd.askdirectory(parent=parent, title='Sélectionner un dossier CDN', initialdir=file_parent)
            if d:
                if isfile(join(d, 'tmd')):
                    success, reason = self.add_cia(d)
                    if not success:
                        self.show_error(f"Impossible d'ajouter {basename(d)} : {reason}")
                    else:
                        self.sort_treeview()
                else:
                    self.show_error('Fichier tmd introuvable dans le dossier CDN :\n' + d)

        add_cdn = ttk.Button(btn_row, text='Ajouter CDN', command=add_cdn_callback)
        add_cdn.grid(row=0, column=1, padx=3)

        def add_dirs_callback():
            d = fd.askdirectory(parent=parent, title='Sélectionner un dossier contenant des CIA',
                                initialdir=file_parent)
            if d:
                results = {}
                for f in scandir(d):
                    if f.name.lower().endswith('.cia'):
                        success, reason = self.add_cia(f.path)
                        if not success:
                            results[f.path] = reason
                if results:
                    TitleReadFailResults(self.parent, failed=results).focus()
                self.sort_treeview()

        add_dirs = ttk.Button(btn_row, text='Ajouter dossier', command=add_dirs_callback)
        add_dirs.grid(row=0, column=2, padx=3)

        def remove_selected_callback():
            for entry in self.treeview.selection():
                self.remove_cia(entry)

        remove_selected = ttk.Button(btn_row, text='Supprimer sélection', command=remove_selected_callback)
        remove_selected.grid(row=0, column=3, padx=3)

        # Chargement depuis un pack
        pack_frame = ttk.LabelFrame(cia_frame, text='Charger depuis un pack')
        pack_frame.grid(row=1, column=0, sticky=tk.EW, padx=5, pady=2)

        ttk.Label(pack_frame, text='Taille :').grid(row=0, column=0, padx=(8, 2), pady=4)
        pack_sizes = self.config.get('cia_pack_order', ['32 Go', '64 Go', '128 Go', '256 Go'])
        self._pack_size_var = tk.StringVar(value=pack_sizes[1] if len(pack_sizes) > 1 else (pack_sizes[0] if pack_sizes else ''))
        ttk.Combobox(pack_frame, textvariable=self._pack_size_var, values=pack_sizes,
                     state='readonly', width=8).grid(row=0, column=1, padx=2)

        ttk.Label(pack_frame, text='Langue :').grid(row=0, column=2, padx=(12, 2))
        first_pack = next(iter(self.config.get('cia_packs', {}).values()), {})
        cia_langs = [k for k in first_pack if k != 'Base']
        self._pack_lang_var = tk.StringVar(value=cia_langs[0] if cia_langs else '')
        ttk.Combobox(pack_frame, textvariable=self._pack_lang_var, values=cia_langs,
                     state='readonly', width=6).grid(row=0, column=3, padx=2)

        ttk.Button(pack_frame, text='Charger', command=self._load_pack).grid(row=0, column=4, padx=(12, 8))

        # Treeview
        treeview_frame = ttk.Frame(cia_frame)
        treeview_frame.grid(row=2, column=0, sticky=tk.NSEW, padx=5, pady=(2, 5))
        treeview_frame.rowconfigure(0, weight=1)
        treeview_frame.columnconfigure(0, weight=1)

        treeview_scrollbar = ttk.Scrollbar(treeview_frame, orient=tk.VERTICAL)
        treeview_scrollbar.grid(row=0, column=1, sticky=tk.NSEW)

        self.treeview = ttk.Treeview(treeview_frame, yscrollcommand=treeview_scrollbar.set)
        self.treeview.grid(row=0, column=0, sticky=tk.NSEW)
        self.treeview.configure(columns=('filepath', 'titleid', 'titlename', 'status'), show='headings')

        self.treeview.column('filepath', width=200, anchor=tk.W)
        self.treeview.heading('filepath', text='Fichier')
        self.treeview.column('titleid', width=70, anchor=tk.W)
        self.treeview.heading('titleid', text='ID Titre')
        self.treeview.column('titlename', width=150, anchor=tk.W)
        self.treeview.heading('titlename', text='Nom')
        self.treeview.column('status', width=20, anchor=tk.W)
        self.treeview.heading('status', text='Statut')

        treeview_scrollbar.configure(command=self.treeview.yview)

        # ---------------------------------------------------------------- #
        # Progression
        self.progressbar = ttk.Progressbar(self, orient=tk.HORIZONTAL, mode='determinate')
        self.progressbar.grid(row=2, column=0, sticky=tk.EW, padx=10, pady=2)

        # ---------------------------------------------------------------- #
        # Options
        opts_frame = ttk.LabelFrame(self, text='Options')
        opts_frame.grid(row=3, column=0, sticky=tk.EW, padx=10, pady=5)

        self.skip_contents_var = tk.IntVar()
        ttk.Checkbutton(opts_frame, text='Sans contenu (base de données uniquement)',
                        variable=self.skip_contents_var).grid(row=0, column=0, sticky=tk.W, padx=5, pady=(5, 2))

        self.overwrite_saves_var = tk.IntVar()
        ttk.Checkbutton(opts_frame, text='Écraser les sauvegardes existantes',
                        variable=self.overwrite_saves_var).grid(row=0, column=1, sticky=tk.W, padx=5, pady=(5, 2))

        nds_sub = ttk.Frame(opts_frame)
        nds_sub.grid(row=1, column=0, columnspan=2, sticky=tk.EW, padx=5, pady=(0, 5))

        self._nds_enabled_var = tk.IntVar()
        ttk.Checkbutton(nds_sub, text='Copier aussi les jeux NDS',
                        variable=self._nds_enabled_var,
                        command=self._toggle_nds).grid(row=0, column=0, padx=(0, 8))

        self._nds_auto_label = ttk.Label(nds_sub, text='', foreground='grey')
        self._nds_auto_label.grid(row=0, column=1, padx=(0, 8))

        ttk.Label(nds_sub, text='Langue :').grid(row=0, column=2, padx=(0, 4))
        first_nds = next(iter(self.config.get('nds_packs', {}).values()), {})
        nds_langs = list(first_nds.get('languages', {}).keys())
        self._nds_lang_var = tk.StringVar(value=nds_langs[0] if nds_langs else '')
        self._nds_lang_combo = ttk.Combobox(nds_sub, textvariable=self._nds_lang_var,
                                             values=nds_langs, state=tk.DISABLED, width=12)
        self._nds_lang_combo.grid(row=0, column=3)

        # ---------------------------------------------------------------- #
        # Statut et boutons
        bottom_frame = ttk.Frame(self)
        bottom_frame.grid(row=4, column=0, sticky=tk.EW, padx=10, pady=(0, 10))
        bottom_frame.columnconfigure(0, weight=1)

        self.status_label = ttk.Label(bottom_frame, text='En attente...')
        self.status_label.grid(row=0, column=0, sticky=tk.W)

        show_console = ttk.Button(bottom_frame, text='Console', command=self.open_console)
        show_console.grid(row=0, column=1, padx=5)

        start = ttk.Button(bottom_frame, text='Installer', command=self.start_install)
        start.grid(row=0, column=2)

        self.log(f'custom-install {__version__} - https://github.com/ihaveamac/custom-install', status=False)

        if is_windows and not taskbar:
            self.log('Note: bibliothèque taskbar introuvable.')
            self.log('Note: la progression ne sera pas visible dans la barre des tâches Windows.')

        self.log('Prêt.')

        self.require_boot9 = (add_cias, add_cdn, add_dirs, remove_selected, start)

        self.disable_buttons()
        self.check_b9_loaded()
        self.enable_buttons()
        if not self.b9_loaded:
            self.log("Note: boot9 non détecté automatiquement. Veuillez le sélectionner avant d'ajouter des titres.")

        self._pack_size_var.trace_add('write', lambda *_: self._update_nds_label())
        self._update_nds_label()

    def sort_treeview(self):
        l = [(self.treeview.set(k, 'titlename'), k) for k in self.treeview.get_children()]
        # sort by title name
        l.sort(key=lambda x: x[0].lower())

        for idx, pair in enumerate(l):
            self.treeview.move(pair[1], '', idx)

    def check_b9_loaded(self):
        if not self.b9_loaded:
            boot9 = self.file_picker_textboxes['boot9'].get('1.0', tk.END).strip()
            try:
                tmp_crypto = CryptoEngine(boot9=boot9)
                self.b9_loaded = tmp_crypto.b9_keys_set
            except:
                return False
        return self.b9_loaded

    def update_status(self, path: 'Union[PathLike, bytes, str]', status: InstallStatus):
        self.treeview.set(path, 'status', statuses[status])

    def add_cia(self, path):
        if not self.check_b9_loaded():
            # this shouldn't happen
            return False, 'Please choose boot9 first'
        path = abspath(path)
        if path in self.readers:
            return False, 'File already in list'
        try:
            reader = CustomInstall.get_reader(path)
        except (CIAError, CDNError, TitleMetadataError):
            return False, 'Failed to read as a CIA or CDN title, probably corrupt'
        except MissingSeedError:
            return False, 'Latest seeddb.bin is required, check the README for details'
        except Exception as e:
            return False, f'Exception occurred: {type(e).__name__}: {e}'

        if reader.tmd.title_id.startswith('00048'):
            return False, 'DSiWare is not supported'
        try:
            title_name = reader.contents[0].exefs.icon.get_app_title().short_desc
        except:
            title_name = '(No title)'
        self.treeview.insert('', tk.END, text=path, iid=path,
                             values=(path, reader.tmd.title_id, title_name, statuses[InstallStatus.Waiting]))
        self.readers[path] = reader
        return True, ''

    def remove_cia(self, path):
        self.treeview.delete(path)
        del self.readers[path]

    def open_console(self):
        if self.console:
            self.console.parent.lift()
            self.console.focus()
        else:
            console_window = tk.Toplevel()
            console_window.title('custom-install Console')

            self.console = ConsoleFrame(console_window, self.log_messages)
            self.console.pack(fill=tk.BOTH, expand=True)

            def close():
                with self.lock:
                    try:
                        console_window.destroy()
                    except:
                        pass
                    self.console = None

            console_window.focus()

            console_window.protocol('WM_DELETE_WINDOW', close)

    def log(self, line, status=True):
        with self.lock:
            log_msg = f"{strftime('%H:%M:%S')} - {line}"
            self.log_messages.append(log_msg)
            if self.console:
                self.console.log(log_msg)

            if status:
                self.status_label.config(text=line)

            print(log_msg)

    def show_error(self, message):
        mb.showerror('Error', message, parent=self.parent)

    def ask_warning(self, message):
        return mb.askokcancel('Warning', message, parent=self.parent)

    def show_info(self, message):
        mb.showinfo('Info', message, parent=self.parent)

    def disable_buttons(self):
        for b in self.require_boot9:
            b.config(state=tk.DISABLED)
        for b in self.file_picker_textboxes.values():
            b.config(state=tk.DISABLED)

    def enable_buttons(self):
        if self.b9_loaded:
            for b in self.require_boot9:
                b.config(state=tk.NORMAL)
        for b in self.file_picker_textboxes.values():
            b.config(state=tk.NORMAL)

    def _load_pack(self):
        source_root = self.config.get('source_root', '').strip()
        if not source_root:
            self.show_error("Le dossier source commun n'est pas configuré.\n"
                            "Allez dans l'onglet Paramètres > Général.")
            return

        size = self._pack_size_var.get()
        language = self._pack_lang_var.get()
        pack_order = self.config.get('cia_pack_order', [])
        cia_packs = self.config.get('cia_packs', {})

        if size not in pack_order:
            self.show_error(f'Taille de pack inconnue : {size}')
            return

        sizes_to_include = pack_order[:pack_order.index(size) + 1]

        folders = []
        for s in sizes_to_include:
            variants = cia_packs.get(s, {})
            pack_folder = variants.get('folder', '').strip()
            for key in ('Base', language):
                subfolder = variants.get(key, '').strip()
                if subfolder:
                    if pack_folder:
                        folders.append(join(source_root, pack_folder, subfolder))
                    else:
                        # Compatibilité ancien format (chemin complet dans chaque variante)
                        folders.append(join(source_root, subfolder))

        added = 0
        failed = {}
        for folder in folders:
            if not isdir(folder):
                self.log(f'Dossier introuvable (ignoré) : {folder}')
                continue
            for entry in scandir(folder):
                if entry.name.lower().endswith('.cia') and entry.is_file():
                    success, reason = self.add_cia(entry.path)
                    if success:
                        added += 1
                    elif reason != 'File already in list':
                        failed[entry.path] = reason

        self.sort_treeview()
        self.log(f'Pack {size} ({language}) chargé : {added} CIA ajoutés.')
        if failed:
            TitleReadFailResults(self.parent, failed=failed).focus()

    def _toggle_nds(self):
        state = 'readonly' if self._nds_enabled_var.get() else tk.DISABLED
        self._nds_lang_combo.config(state=state)

    def _update_id0(self, path):
        """Lancer l'extraction de l'ID0 depuis movable.sed dans un thread de fond."""
        if not path or not isfile(path):
            self._id0_label.config(text='(fichier introuvable)', foreground='red')
            return
        self._id0_label.config(text='Lecture...', foreground='grey')
        def _do():
            id0 = _try_get_id0(path)
            def _set():
                if id0:
                    self._id0_label.config(text=id0, foreground='#005500')
                else:
                    self._id0_label.config(text='(impossible de lire le movable.sed)', foreground='red')
            self.after(0, _set)
        Thread(target=_do, daemon=True).start()

    def _update_nds_label(self):
        cia_size = self._pack_size_var.get()
        nds_size = self.config.get('cia_to_nds', {}).get(cia_size, '?')
        self._nds_auto_label.config(text=f'→ Pack NDS : {nds_size}')

    def _run_nds_copy(self, target_path):
        """Copie les jeux NDS vers target_path (appelé depuis le thread d'installation)."""
        source_root = (self.config.get('nds_source_root', '').strip()
                       or self.config.get('source_root', '').strip())
        if not source_root:
            self.log('Copie NDS ignorée : dossier source non configuré dans les Paramètres.')
            return

        cia_size = self._pack_size_var.get()
        nds_size = self.config.get('cia_to_nds', {}).get(cia_size)
        if not nds_size:
            self.log(f'Copie NDS ignorée : pas de mapping NDS pour {cia_size}.')
            return

        nds_packs = self.config.get('nds_packs', {})
        nds_pack_order = self.config.get('nds_pack_order', list(nds_packs.keys()))
        language = self._nds_lang_var.get()

        # Logique additive : tous les packs jusqu'au pack cible inclus
        try:
            packs_to_copy = nds_pack_order[:nds_pack_order.index(nds_size) + 1]
        except ValueError:
            packs_to_copy = [nds_size]

        # --- Vérification de l'espace disponible ---
        self.log("Calcul de l'espace NDS requis...")
        total_nds_size = 0
        for pack_name in packs_to_copy:
            nds_pack = nds_packs.get(pack_name)
            if not nds_pack:
                continue
            pack_root = join(source_root, nds_pack.get('folder', '').rstrip('/\\'))
            base_dir = join(pack_root, nds_pack.get('base_folder', 'Base NDS'))
            if isdir(base_dir):
                for dp, _, fns in walk(base_dir):
                    for fn in fns:
                        try:
                            total_nds_size += getsize(join(dp, fn))
                        except OSError:
                            pass
            lang_folder = nds_pack.get('languages', {}).get(language, '')
            if lang_folder:
                lang_dir = join(pack_root, lang_folder)
                if isdir(lang_dir):
                    for dp, _, fns in walk(lang_dir):
                        for fn in fns:
                            try:
                                total_nds_size += getsize(join(dp, fn))
                            except OSError:
                                pass
        try:
            free = shutil.disk_usage(target_path).free
            if total_nds_size > free:
                needed_gb = total_nds_size / (1024 ** 3)
                free_gb = free / (1024 ** 3)
                self.log(f'Copie NDS annulée : {needed_gb:.2f} Go requis, {free_gb:.2f} Go disponibles.')
                self.after(0, lambda n=needed_gb, f=free_gb: self.show_error(
                    f'Espace insuffisant pour la copie NDS :\n'
                    f'Requis : {n:.2f} Go\nDisponible : {f:.2f} Go'))
                return
        except OSError:
            pass  # disque inaccessible : on tente quand même

        # --- Copie ---
        self.log(f'Copie NDS ({language}) – {" + ".join(packs_to_copy)}...')
        self.after(0, lambda: self.progressbar.config(maximum=100, value=0))
        cancelled = [False]

        def on_progress(copied, total, speed, filename):
            # Appelé depuis le thread de copie → after() obligatoire
            pct = (copied / total * 100) if total > 0 else 0
            def _upd(p=pct, s=speed, f=filename):
                self.progressbar.config(value=p)
                self.status_label.config(
                    text=f'NDS {p:.1f}%  —  {s / (1024 ** 2):.1f} Mo/s  —  {f}')
            self.after(0, _upd)

        def on_error(message):
            self.log(f'Erreur NDS : {message}')
            self.after(0, lambda m=message: self.show_error(
                f'Erreur lors de la copie NDS :\n{m}'))

        for pack_name in packs_to_copy:
            if cancelled[0]:
                break
            nds_pack = nds_packs.get(pack_name)
            if not nds_pack:
                self.log(f'Pack NDS "{pack_name}" introuvable, ignoré.')
                continue
            if len(packs_to_copy) > 1:
                self.log(f'▶ Pack NDS {pack_name}...')
            copier = NDSCopier(
                source_root=join(source_root, nds_pack.get('folder', '').rstrip('/\\')),
                base_folder=nds_pack.get('base_folder', 'Base NDS'),
                language_folders=nds_pack.get('languages', {}),
            )
            copier.event.on_log += self.log
            copier.event.on_progress += on_progress
            copier.event.on_done += lambda e, s, d, c: (
                self.log(f'  {int(e)//60}m{int(e)%60:02d}s — {s/(1024**2):.1f} Mo/s')
                if not c else None
            )
            copier.event.on_error += on_error
            copier.start(language, target_path, cancelled)

        if not cancelled[0]:
            self.log(f'Jeux NDS copiés → {target_path}')

    def start_install(self):
        sd_root = self.file_picker_textboxes['sd'].get('1.0', tk.END).strip()
        seeddb = self.file_picker_textboxes['seeddb'].get('1.0', tk.END).strip()
        movable_sed = self.file_picker_textboxes['movable.sed'].get('1.0', tk.END).strip()

        if not sd_root:
            self.show_error('SD root is not specified.')
            return
        if not movable_sed:
            self.show_error('movable.sed is not specified.')
            return

        if not seeddb:
            if not self.ask_warning('seeddb was not specified. Titles that require it will fail to install.\n'
                                    'Continue?'):
                return

        if not len(self.readers):
            self.show_error('There are no titles added to install.')
            return

        try:
            installer = CustomInstall(movable=movable_sed,
                                      sd=sd_root,
                                      skip_contents=self.skip_contents_var.get() == 1,
                                      overwrite_saves=self.overwrite_saves_var.get() == 1)
        except Exception as e:
            self.show_error(f'Impossible de préparer l\'installation :\n{type(e).__name__}: {e}')
            return

        if not installer.check_for_id0():
            self.show_error(f'id0 {installer.crypto.id0.hex()} was not found inside "Nintendo 3DS" on the SD card.\n'
                            f'\n'
                            f'Before using custom-install, you should use this SD card on the appropriate console.\n'
                            f'\n'
                            f'Otherwise, make sure the correct movable.sed is being used.')
            return

        for path in self.readers.keys():
            self.update_status(path, InstallStatus.Waiting)
        self.disable_buttons()

        if taskbar:
            taskbar.SetProgressState(self.hwnd, tbl.TBPF_NORMAL)

        self.log('Starting install...')

        # use the treeview which has been sorted alphabetically
        readers_final = []
        for k in self.treeview.get_children():
            filepath = self.treeview.set(k, 'filepath')
            readers_final.append((self.readers[filepath], filepath))

        installer.readers = readers_final

        finished_percent = 0
        max_percentage = 100 * len(self.readers)
        self.progressbar.config(maximum=max_percentage)

        def ci_on_log_msg(message, *args, **kwargs):
            # ignoring end
            self.log(message)

        def ci_update_percentage(total_percent, total_read, size):
            self.progressbar.config(value=total_percent + finished_percent)
            if taskbar:
                taskbar.SetProgressValue(self.hwnd, int(total_percent + finished_percent), max_percentage)

        def ci_on_error(exc):
            if taskbar:
                taskbar.SetProgressState(self.hwnd, tbl.TBPF_ERROR)
            for line in format_exception(*exc):
                for line2 in line.split('\n')[:-1]:
                    installer.log(line2)
            self.show_error('An error occurred during installation.')
            self.open_console()

        def ci_on_cia_start(idx):
            nonlocal finished_percent
            finished_percent = idx * 100
            if taskbar:
                taskbar.SetProgressValue(self.hwnd, finished_percent, max_percentage)

        installer.event.on_log_msg += ci_on_log_msg
        installer.event.update_percentage += ci_update_percentage
        installer.event.on_error += ci_on_error
        installer.event.on_cia_start += ci_on_cia_start
        installer.event.update_status += self.update_status

        if self.skip_contents_var.get() != 1:
            total_size, free_space = installer.check_size()
            if total_size > free_space:
                self.show_error(f'Not enough free space.\n'
                                f'Combined title install size: {total_size / (1024 * 1024):0.2f} MiB\n'
                                f'Free space: {free_space / (1024 * 1024):0.2f} MiB')
                self.enable_buttons()
                return

        def install():
            try:
                result, copied_3dsx, application_count = installer.start()
                # Toutes les mises à jour d'interface DOIVENT passer par after() :
                # créer un Toplevel depuis un thread secondaire crashe Tkinter.
                def _show_result(r=result, c=copied_3dsx, a=application_count):
                    if r:
                        rw = InstallResults(self.parent,
                                            install_state=r,
                                            copied_3dsx=c,
                                            application_count=a)
                        rw.focus()
                    elif r is None:
                        self.show_error("An error occurred when trying to run save3ds_fuse.\n"
                                        "Either title.db doesn't exist, or save3ds_fuse couldn't be run.")
                        self.open_console()
                self.after(0, _show_result)
                if result and self._nds_enabled_var.get():
                    self._run_nds_copy(sd_root)
            except:
                exc = sys.exc_info()
                self.after(0, lambda e=exc: installer.event.on_error(e))
            finally:
                self.after(0, self.enable_buttons)

        Thread(target=install).start()


class NDSCopyFrame(ttk.Frame):
    def __init__(self, parent, config):
        super().__init__(parent)
        self.config = config
        self._cancelled = [False]

        self.rowconfigure(2, weight=1)
        self.columnconfigure(0, weight=1)

        # --- Destination ---
        dest_frame = ttk.LabelFrame(self, text='Destination (carte SD)')
        dest_frame.grid(row=0, column=0, sticky=tk.EW, padx=10, pady=10)

        ttk.Label(dest_frame, text='Lecteur :').grid(row=0, column=0, sticky=tk.W, padx=(8, 4), pady=5)

        # Subframe : combo + ↻ collés ensemble
        _dc = ttk.Frame(dest_frame)
        _dc.grid(row=0, column=1, sticky=tk.W, padx=(0, 8), pady=5)

        self._drive_combo = ttk.Combobox(_dc, values=[_DRIVE_NONE], state='readonly', width=40)
        self._drive_combo.current(0)  # (Aucun) par défaut
        self._drive_combo.grid(row=0, column=0, padx=(0, 3))
        _refresh_nds = _setup_drive_combo(self, self._drive_combo, config)

        ttk.Button(_dc, text='↻', width=3, command=_refresh_nds).grid(row=0, column=1)

        # --- Options ---
        opts_frame = ttk.LabelFrame(self, text='Options')
        opts_frame.grid(row=1, column=0, sticky=tk.EW, padx=10, pady=(0, 10))
        opts_frame.columnconfigure(1, weight=1)

        ttk.Label(opts_frame, text='Pack NDS :').grid(row=0, column=0, sticky=tk.W, padx=5, pady=5)
        nds_pack_names = list(config.get('nds_packs', {}).keys())
        self._nds_pack_var = tk.StringVar(value=nds_pack_names[0] if nds_pack_names else '')
        self._nds_pack_combo = ttk.Combobox(opts_frame, textvariable=self._nds_pack_var,
                                             values=nds_pack_names, state='readonly', width=10)
        self._nds_pack_combo.grid(row=0, column=1, sticky=tk.W, padx=5)
        self._nds_pack_combo.bind('<<ComboboxSelected>>', self._on_pack_changed)

        ttk.Label(opts_frame, text='Langue :').grid(row=1, column=0, sticky=tk.W, padx=5, pady=5)
        first_nds = next(iter(config.get('nds_packs', {}).values()), {})
        langs = list(first_nds.get('languages', {}).keys())
        self._lang_var = tk.StringVar(value=langs[0] if langs else '')
        self._lang_combo = ttk.Combobox(opts_frame, textvariable=self._lang_var,
                                         values=langs, state='readonly', width=15)
        self._lang_combo.grid(row=1, column=1, sticky=tk.W, padx=5)

        # --- Progression ---
        prog_frame = ttk.LabelFrame(self, text='Progression')
        prog_frame.grid(row=2, column=0, sticky=tk.NSEW, padx=10, pady=(0, 10))
        prog_frame.rowconfigure(3, weight=1)
        prog_frame.columnconfigure(0, weight=1)

        self._progress_var = tk.DoubleVar()
        ttk.Progressbar(prog_frame, variable=self._progress_var, maximum=100).grid(
            row=0, column=0, columnspan=2, sticky=tk.EW, padx=10, pady=(5, 0))

        self._progress_label = ttk.Label(prog_frame, text='En attente...')
        self._progress_label.grid(row=1, column=0, sticky=tk.W, padx=10)

        self._file_label = ttk.Label(prog_frame, text='', foreground='grey')
        self._file_label.grid(row=2, column=0, sticky=tk.W, padx=10)

        log_scroll = ttk.Scrollbar(prog_frame, orient=tk.VERTICAL)
        log_scroll.grid(row=3, column=1, sticky=tk.NSEW)
        self._log_text = tk.Text(prog_frame, height=8, state=tk.DISABLED,
                                  wrap='word', yscrollcommand=log_scroll.set)
        self._log_text.grid(row=3, column=0, sticky=tk.NSEW, padx=(10, 0), pady=5)
        log_scroll.config(command=self._log_text.yview)

        # --- Boutons ---
        btn_frame = ttk.Frame(self)
        btn_frame.grid(row=3, column=0, pady=10)

        self._start_btn = ttk.Button(btn_frame, text='Démarrer la copie', command=self._start)
        self._start_btn.grid(row=0, column=0, padx=5)

        self._cancel_btn = ttk.Button(btn_frame, text='Annuler', command=self._cancel,
                                       state=tk.DISABLED)
        self._cancel_btn.grid(row=0, column=1, padx=5)

        self._last_progress_ts = 0.0  # throttle : max 10 mises à jour/sec

    def _log(self, message):
        # Peut être appelé depuis n'importe quel thread → planifié sur le thread principal
        def _do():
            self._log_text.configure(state=tk.NORMAL)
            self._log_text.insert(tk.END, message + '\n')
            self._log_text.see(tk.END)
            self._log_text.configure(state=tk.DISABLED)
        self.after(0, _do)

    def _update_progress(self, copied, total, speed, filename):
        # Appelé depuis le thread NDSCopier → throttle à 10/sec pour ne pas saturer la queue
        now = time.monotonic()
        if now - self._last_progress_ts < 0.1:
            return
        self._last_progress_ts = now
        pct = (copied / total * 100) if total > 0 else 0
        text = (f'{pct:.1f}%  —  {copied / (1024 ** 3):.2f} Go / {total / (1024 ** 3):.2f} Go'
                f'  —  {speed / (1024 ** 2):.1f} Mo/s')
        self.after(0, lambda p=pct, t=text, f=filename: (
            self._progress_var.set(p),
            self._progress_label.config(text=t),
            self._file_label.config(text=f),
        ))

    def _on_error(self, message):
        def _do():
            mb.showerror('Erreur', message, parent=self)
            self._log_text.configure(state=tk.NORMAL)
            self._log_text.insert(tk.END, f'Erreur : {message}\n')
            self._log_text.see(tk.END)
            self._log_text.configure(state=tk.DISABLED)
            self._start_btn.config(state=tk.NORMAL)
            self._cancel_btn.config(state=tk.DISABLED)
        self.after(0, _do)

    def _on_pack_changed(self, event=None):
        pack_name = self._nds_pack_var.get()
        nds_pack = self.config.get('nds_packs', {}).get(pack_name, {})
        langs = list(nds_pack.get('languages', {}).keys())
        self._lang_combo.configure(values=langs)
        self._lang_var.set(langs[0] if langs else '')

    def _start(self):
        dest = self._drive_combo.get().strip()
        language = self._lang_var.get()
        source_root = (self.config.get('nds_source_root', '').strip()
                       or self.config.get('source_root', '').strip())
        nds_packs = self.config.get('nds_packs', {})
        nds_pack_order = self.config.get('nds_pack_order', list(nds_packs.keys()))
        selected_pack = self._nds_pack_var.get()

        target_path = _drive_to_path(dest) if is_windows else dest

        if not target_path:
            mb.showerror('Erreur', 'Veuillez sélectionner un lecteur de destination.', parent=self)
            return
        if not language:
            mb.showerror('Erreur', 'Veuillez sélectionner une langue.', parent=self)
            return
        if not source_root:
            mb.showerror('Erreur',
                         "Le dossier source n'est pas configuré.\n"
                         "Allez dans l'onglet Paramètres.", parent=self)
            return

        # Logique additive : tous les packs jusqu'au sélectionné inclus
        try:
            packs_to_copy = nds_pack_order[:nds_pack_order.index(selected_pack) + 1]
        except ValueError:
            packs_to_copy = [selected_pack]

        self._cancelled = [False]
        self._log_text.configure(state=tk.NORMAL)
        self._log_text.delete('1.0', tk.END)
        self._log_text.configure(state=tk.DISABLED)
        self._progress_var.set(0)
        self._progress_label.config(text='Démarrage...')
        self._file_label.config(text='')
        self._start_btn.config(state=tk.DISABLED)
        self._cancel_btn.config(state=tk.NORMAL)

        cancelled = self._cancelled
        config_snap = self.config  # snapshot pour le thread

        def do_copy():
            try:
                _do_copy()
            except Exception as e:
                self._on_error(f'Copie interrompue : {type(e).__name__}: {e}')

        def _do_copy():
            # Récapitulatif du mode additif (affiché avant tout pour éviter la confusion)
            if len(packs_to_copy) > 1:
                self._log(
                    f'Mode additif — pack sélectionné : {selected_pack}\n'
                    f'  Packs copiés (du plus petit au plus grand) : {" → ".join(packs_to_copy)}'
                )
            # --- Vérification de l'espace disponible ---
            self._log("Calcul de l'espace requis...")
            total_nds_size = 0
            for pack_name in packs_to_copy:
                nds_pack = config_snap.get('nds_packs', {}).get(pack_name)
                if not nds_pack:
                    continue
                pack_root = join(source_root, nds_pack.get('folder', '').rstrip('/\\'))
                base_dir = join(pack_root, nds_pack.get('base_folder', 'Base NDS'))
                if isdir(base_dir):
                    for dp, _, fns in walk(base_dir):
                        for fn in fns:
                            try:
                                total_nds_size += getsize(join(dp, fn))
                            except OSError:
                                pass
                lang_folder = nds_pack.get('languages', {}).get(language, '')
                if lang_folder:
                    lang_dir = join(pack_root, lang_folder)
                    if isdir(lang_dir):
                        for dp, _, fns in walk(lang_dir):
                            for fn in fns:
                                try:
                                    total_nds_size += getsize(join(dp, fn))
                                except OSError:
                                    pass
            try:
                free = shutil.disk_usage(target_path).free
                if total_nds_size > free:
                    needed_gb = total_nds_size / (1024 ** 3)
                    free_gb = free / (1024 ** 3)
                    self._log(f'Espace insuffisant : {needed_gb:.2f} Go requis, {free_gb:.2f} Go disponibles.')
                    def _abort_nds(n=needed_gb, f2=free_gb):
                        mb.showerror('Espace insuffisant',
                                     f'Espace requis : {n:.2f} Go\n'
                                     f'Espace disponible : {f2:.2f} Go\n\n'
                                     'Annulation de la copie NDS.', parent=self)
                        self._start_btn.config(state=tk.NORMAL)
                        self._cancel_btn.config(state=tk.DISABLED)
                    self.after(0, _abort_nds)
                    return
            except OSError:
                pass  # Si le disque n'est pas accessible, on tente quand même

            # --- Copie ---
            for pack_name in packs_to_copy:
                if cancelled[0]:
                    break
                nds_pack = config_snap.get('nds_packs', {}).get(pack_name)
                if not nds_pack:
                    self._log(f'Pack "{pack_name}" introuvable, ignoré.')
                    continue
                if len(packs_to_copy) > 1:
                    self._log(f'▶ Pack NDS {pack_name}...')
                copier = NDSCopier(
                    source_root=join(source_root, nds_pack.get('folder', '').rstrip('/\\')),
                    base_folder=nds_pack.get('base_folder', 'Base NDS'),
                    language_folders=nds_pack.get('languages', {}),
                )
                copier.event.on_log += self._log
                copier.event.on_progress += self._update_progress
                copier.event.on_done += lambda e, s, d, c: (
                    self._log(f'  {int(e)//60}m{int(e)%60:02d}s — {s/(1024**2):.1f} Mo/s')
                    if not c else None
                )
                copier.event.on_error += lambda msg: self._log(f'Erreur : {msg}')
                copier.start(language, target_path, cancelled)

            if cancelled[0]:
                self._log('Copie annulée.')
            else:
                self._log(f'Copie NDS terminée → {target_path}')
            self.after(0, lambda: (
                self._start_btn.config(state=tk.NORMAL),
                self._cancel_btn.config(state=tk.DISABLED),
            ))

        Thread(target=do_copy, daemon=True).start()

    def _cancel(self):
        self._cancelled[0] = True
        self._cancel_btn.config(state=tk.DISABLED)
        self._log('Annulation en cours...')


class CustomPackFrame(ttk.Frame):
    NUM_PACKS = 5

    def __init__(self, parent, config):
        super().__init__(parent)
        self.config = config
        self._cancelled = [False]

        self.rowconfigure(2, weight=1)
        self.columnconfigure(0, weight=1)

        # --- Destination ---
        dest_frame = ttk.LabelFrame(self, text='Destination (carte SD)')
        dest_frame.grid(row=0, column=0, sticky=tk.EW, padx=10, pady=10)

        ttk.Label(dest_frame, text='Lecteur :').grid(row=0, column=0, sticky=tk.W, padx=(8, 4), pady=5)
        _dc2 = ttk.Frame(dest_frame)
        _dc2.grid(row=0, column=1, sticky=tk.W, padx=(0, 8), pady=5)

        # C:, D: et les disques au-delà de la limite de taille (Paramètres) ne sont pas proposés
        self._drive_combo = ttk.Combobox(_dc2, values=[_DRIVE_NONE], state='readonly', width=40)
        self._drive_combo.current(0)  # (Aucun) par défaut
        self._drive_combo.grid(row=0, column=0, padx=(0, 3))
        ttk.Button(_dc2, text='↻', width=3,
                   command=_setup_drive_combo(self, self._drive_combo, config)).grid(row=0, column=1)

        # --- Packs ---
        packs_frame = ttk.LabelFrame(self, text='Packs à copier (le contenu sera copié à la racine de la SD)')
        packs_frame.grid(row=1, column=0, sticky=tk.EW, padx=10, pady=(0, 10))
        packs_frame.columnconfigure(2, weight=1)  # colonne chemin extensible

        # En-tête des colonnes
        ttk.Label(packs_frame, text='Nom du pack', foreground='grey').grid(
            row=0, column=1, padx=(2, 5), pady=(4, 0), sticky=tk.W)
        ttk.Label(packs_frame, text='Chemin source', foreground='grey').grid(
            row=0, column=2, padx=5, pady=(4, 0), sticky=tk.W)

        # Chargement : formats supportés :
        #   {'path':..,'enabled':..,'name':..}  (nouveau)
        #   {'path':..,'enabled':..}            (intermédiaire)
        #   chaîne directe                      (ancien)
        def _parse_pack(item):
            if isinstance(item, dict):
                return (bool(item.get('enabled', False)),
                        str(item.get('name', '')),
                        str(item.get('path', '')))
            return bool(item), '', str(item)

        saved_packs = config.get('custom_packs', [])
        self._pack_entries = []

        for i in range(self.NUM_PACKS):
            raw = saved_packs[i] if i < len(saved_packs) else ''
            enabled_init, name_init, path_init = _parse_pack(raw)
            enabled_var = tk.IntVar(value=int(enabled_init))
            name_var = tk.StringVar(value=name_init)
            path_var = tk.StringVar(value=path_init)
            row = i + 1  # ligne 0 = en-tête

            ttk.Checkbutton(packs_frame, text=f'{i + 1}',
                            variable=enabled_var).grid(row=row, column=0, sticky=tk.W,
                                                       padx=(5, 2), pady=3)
            name_entry = ttk.Entry(packs_frame, textvariable=name_var, width=16)
            name_entry.grid(row=row, column=1, padx=(2, 5), pady=3)
            name_entry.bind('<FocusOut>', lambda e: self._save_paths())

            path_entry = ttk.Entry(packs_frame, textvariable=path_var)
            path_entry.grid(row=row, column=2, sticky=tk.EW, padx=5, pady=3)
            path_entry.bind('<FocusOut>', lambda e: self._save_paths())

            def browse(var=path_var):
                path = fd.askdirectory(parent=self, title='Sélectionner le dossier source',
                                       mustexist=True)
                if path:
                    var.set(path)
                    self._save_paths()

            ttk.Button(packs_frame, text='...', command=browse).grid(
                row=row, column=3, padx=(0, 3), pady=3)
            ttk.Button(packs_frame, text='×', width=2,
                       command=lambda v=path_var, nv=name_var, ev=enabled_var: (
                           v.set(''), nv.set(''), ev.set(0), self._save_paths()
                       )).grid(row=row, column=4, padx=(0, 5), pady=3)
            self._pack_entries.append((enabled_var, name_var, path_var))

        save_row = self.NUM_PACKS + 1  # +1 pour l'en-tête
        self._save_status = ttk.Label(packs_frame, text='', foreground='green')
        self._save_status.grid(row=save_row, column=2, sticky=tk.W, padx=5, pady=(4, 2))
        ttk.Button(packs_frame, text='Enregistrer les chemins',
                   command=self._save_paths).grid(row=save_row, column=3, columnspan=2,
                                                  padx=(0, 5), pady=(4, 2))

        # --- Progression ---
        prog_frame = ttk.LabelFrame(self, text='Progression')
        prog_frame.grid(row=2, column=0, sticky=tk.NSEW, padx=10, pady=(0, 10))
        prog_frame.rowconfigure(3, weight=1)
        prog_frame.columnconfigure(0, weight=1)

        self._progress_var = tk.DoubleVar()
        ttk.Progressbar(prog_frame, variable=self._progress_var, maximum=100).grid(
            row=0, column=0, columnspan=2, sticky=tk.EW, padx=10, pady=(5, 0))

        self._progress_label = ttk.Label(prog_frame, text='En attente...')
        self._progress_label.grid(row=1, column=0, sticky=tk.W, padx=10)

        self._file_label = ttk.Label(prog_frame, text='', foreground='grey')
        self._file_label.grid(row=2, column=0, sticky=tk.W, padx=10)

        log_scroll = ttk.Scrollbar(prog_frame, orient=tk.VERTICAL)
        log_scroll.grid(row=3, column=1, sticky=tk.NSEW)
        self._log_text = tk.Text(prog_frame, height=6, state=tk.DISABLED,
                                  wrap='word', yscrollcommand=log_scroll.set)
        self._log_text.grid(row=3, column=0, sticky=tk.NSEW, padx=(10, 0), pady=5)
        log_scroll.config(command=self._log_text.yview)

        # --- Boutons ---
        btn_frame = ttk.Frame(self)
        btn_frame.grid(row=3, column=0, pady=10)

        self._start_btn = ttk.Button(btn_frame, text='Démarrer la copie', command=self._start)
        self._start_btn.grid(row=0, column=0, padx=5)

        self._cancel_btn = ttk.Button(btn_frame, text='Annuler', command=self._cancel,
                                       state=tk.DISABLED)
        self._cancel_btn.grid(row=0, column=1, padx=5)

    def _save_paths(self):
        self.config['custom_packs'] = [
            {'path': pv.get().strip(), 'enabled': bool(ev.get()), 'name': nv.get().strip()}
            for ev, nv, pv in self._pack_entries
        ]
        save_config(self.config)
        self._save_status.config(text='Enregistré.')
        self.after(2000, lambda: self._save_status.config(text=''))

    def _log(self, message):
        # Peut être appelé depuis le thread de copie → planifié sur le thread principal
        def _do():
            self._log_text.configure(state=tk.NORMAL)
            self._log_text.insert(tk.END, message + '\n')
            self._log_text.see(tk.END)
            self._log_text.configure(state=tk.DISABLED)
        self.after(0, _do)

    def _copy_contents(self, src, target_path, cancelled, state):
        for dirpath, _, filenames in walk(src):
            if cancelled[0]:
                return
            rel = relpath(dirpath, src)
            dest_dir = target_path if rel == '.' else join(target_path, rel)
            makedirs(dest_dir, exist_ok=True)
            for filename in filenames:
                if cancelled[0]:
                    return
                src_file = join(dirpath, filename)
                dst_file = join(dest_dir, filename)
                try:
                    file_size = getsize(src_file)
                except OSError:
                    file_size = 0
                shutil.copy2(src_file, dst_file)
                state['copied'] += file_size
                now = time.time()
                # Throttle : max ~10 mises à jour/sec pour ne pas saturer l'event loop
                if now - state.get('_last_ui', 0) >= 0.1:
                    state['_last_ui'] = now
                    elapsed = now - state['start']
                    speed = state['copied'] / elapsed if elapsed > 0 else 0
                    pct = (state['copied'] / state['total'] * 100) if state['total'] > 0 else 0
                    label = (f'{pct:.1f}%  —  {state["copied"] / (1024 ** 3):.2f} Go'
                             f' / {state["total"] / (1024 ** 3):.2f} Go'
                             f'  —  {speed / (1024 ** 2):.1f} Mo/s')
                    self.after(0, lambda p=pct, t=label, f=filename: (
                        self._progress_var.set(p),
                        self._progress_label.config(text=t),
                        self._file_label.config(text=f),
                    ))

    def _run_copy(self, sources, target_path, cancelled):
        try:
            self._run_copy_inner(sources, target_path, cancelled)
        except Exception as e:
            self.after(0, lambda m=f'Copie interrompue : {type(e).__name__}: {e}': self._abort(m, error=True))

    def _run_copy_inner(self, sources, target_path, cancelled):
        total = 0
        for src in sources:
            if isdir(src):
                for dirpath, _, filenames in walk(src):
                    for f in filenames:
                        try:
                            total += getsize(join(dirpath, f))
                        except OSError:
                            pass
            else:
                self._log(f'Dossier introuvable (ignoré) : {src}')

        self._log(f'Taille totale : {total / (1024 ** 3):.2f} Go')

        # Vérification de l'espace disponible sur la destination
        try:
            free = shutil.disk_usage(target_path).free
            if total > free:
                needed_gb = total / (1024 ** 3)
                free_gb = free / (1024 ** 3)
                self._log(f'Espace insuffisant : {needed_gb:.2f} Go requis, {free_gb:.2f} Go disponibles.')
                def _abort_cp(n=needed_gb, f2=free_gb):
                    mb.showerror('Espace insuffisant',
                                 f'Espace requis : {n:.2f} Go\n'
                                 f'Espace disponible : {f2:.2f} Go\n\n'
                                 'Annulation de la copie.', parent=self)
                    self._start_btn.config(state=tk.NORMAL)
                    self._cancel_btn.config(state=tk.DISABLED)
                self.after(0, _abort_cp)
                return
        except OSError:
            pass  # Si le disque n'est pas accessible, on tente quand même

        state = {'copied': 0, 'total': total, 'start': time.time()}

        for src in sources:
            if cancelled[0]:
                break
            if not isdir(src):
                continue
            self._log(f'Copie de : {src}')
            self._copy_contents(src, target_path, cancelled, state)

        elapsed = time.time() - state['start']
        m, s = divmod(int(elapsed), 60)
        if cancelled[0]:
            self._log('Copie annulée.')
            self.after(0, lambda: self._progress_label.config(text='Annulé.'))
        else:
            avg_speed = state['copied'] / elapsed if elapsed > 0 else 0
            self._log(f'Terminé en {m}m{s:02d}s  —  Débit moyen : {avg_speed / (1024 ** 2):.1f} Mo/s')
            self._log(f'Destination : {target_path}')
            self.after(0, lambda: self._progress_label.config(text='Terminé.'))

        self.after(0, lambda: (
            self._start_btn.config(state=tk.NORMAL),
            self._cancel_btn.config(state=tk.DISABLED),
        ))

    def _start(self):
        target_path = _drive_to_path(self._drive_combo.get()) if is_windows else self._drive_combo.get().strip()
        if not target_path:
            mb.showerror('Erreur', 'Veuillez sélectionner un lecteur.', parent=self)
            return

        sources = [pv.get().strip() for ev, nv, pv in self._pack_entries
                   if ev.get() and pv.get().strip()]
        if not sources:
            mb.showerror('Erreur', 'Aucun pack activé ou configuré.', parent=self)
            return

        if is_windows:
            same_drive = [src for src in sources if abspath(src)[:1].upper() == target_path[:1].upper()]
            if same_drive:
                mb.showerror('Erreur', 'Un pack source se trouve sur le lecteur de destination :\n'
                             f'{same_drive[0]}', parent=self)
                return

        self._save_paths()

        self._cancelled = [False]
        self._log_text.configure(state=tk.NORMAL)
        self._log_text.delete('1.0', tk.END)
        self._log_text.configure(state=tk.DISABLED)
        self._progress_var.set(0)
        self._progress_label.config(text='Vérification de la carte SD...')
        self._file_label.config(text='')
        self._start_btn.config(state=tk.DISABLED)
        self._cancel_btn.config(state=tk.NORMAL)

        cancelled = self._cancelled
        if not is_windows:
            Thread(target=lambda: self._run_copy(sources, target_path, cancelled), daemon=True).start()
            return

        def check():
            # Hors du thread de l'interface : interroge Windows (lecteur autorisé ? format ?)
            try:
                info = sdformat.get_safe_drive(target_path[0], sdformat.max_bytes_from_config(self.config))
            except sdformat.SDFormatError as e:
                self.after(0, lambda m=str(e): self._abort(m, error=True))
                return
            ok, fs, cluster = sdformat.check_sd_format(info.letter)
            self.after(0, lambda: self._ask_prepare(info, ok, fs, cluster, sources, cancelled))

        Thread(target=check, daemon=True).start()

    def _abort(self, message, error=False):
        self._log(message)
        if error:
            mb.showerror('Erreur', message, parent=self)
        self._progress_label.config(text='Annulé.')
        self._start_btn.config(state=tk.NORMAL)
        self._cancel_btn.config(state=tk.DISABLED)

    def _ask_prepare(self, info, format_ok, fs, cluster, sources, cancelled):
        drive = f'{info.letter}:'
        clear = False
        do_format = False

        if not format_ok:
            current = f'{fs or "illisible"}' + (f', clusters de {cluster // 1024} Ko' if cluster else '')
            self._log(f'Format de {drive} incorrect : {current} (attendu : FAT32, clusters de 32 Ko).')
            if not mb.askyesno(
                    'Format de la carte incorrect',
                    f'La carte {drive} ({sdformat.human_size(info.size)}) est en '
                    f'{"format non reconnu" if info.raw else current}.\n'
                    'La 3DS a besoin de FAT32 avec des clusters de 32 Ko.\n\n'
                    f'Formater {drive} maintenant ?\n\n'
                    '⚠ TOUTES les données de la carte seront définitivement effacées.',
                    icon=mb.WARNING, default=mb.NO, parent=self):
                self._abort('Copie annulée : la carte n’est pas au bon format.')
                return
            do_format = True
        else:
            try:
                entries = sorted(e.name for e in scandir(info.root)
                                 if e.name.lower() not in sdformat.KEEP_ON_CLEAR)
            except OSError:
                entries = []
            if entries:
                preview = '\n'.join(f'  • {n}' for n in entries[:8])
                if len(entries) > 8:
                    preview += f'\n  … et {len(entries) - 8} autre(s)'
                answer = mb.askyesnocancel(
                    'Contenu existant sur la carte',
                    f'La carte {drive} contient déjà {len(entries)} élément(s) :\n{preview}\n\n'
                    'Supprimer ce contenu avant de copier le nouveau pack ?\n\n'
                    'Oui : tout supprimer (y compris « Nintendo 3DS » s’il est présent)\n'
                    'Non : conserver et écraser les fichiers existants\n'
                    'Annuler : ne rien faire',
                    icon=mb.WARNING, default=mb.NO, parent=self)
                if answer is None:
                    self._abort('Copie annulée.')
                    return
                if answer:
                    if not mb.askokcancel('Confirmer la suppression',
                                          f'Supprimer définitivement le contenu de {drive} ?',
                                          icon=mb.WARNING, default=mb.CANCEL, parent=self):
                        self._abort('Copie annulée.')
                        return
                    clear = True

        label = sdformat.sanitize_label(info.label) or '3DS'
        max_bytes = sdformat.max_bytes_from_config(self.config)

        def work():
            try:
                if do_format:
                    self.after(0, lambda: self._progress_label.config(text='Formatage en cours...'))
                    sdformat.format_drive_elevated(info.letter, label, info.size, max_bytes, log=self._log)
                elif clear:
                    self.after(0, lambda: self._progress_label.config(text='Suppression du contenu...'))
                    self._log(f'Suppression du contenu de {drive}...')
                    n = sdformat.clear_drive_contents(info.root, max_bytes, log=self._log, cancelled=cancelled)
                    self._log(f'{n} élément(s) supprimé(s).')
            except (sdformat.SDFormatError, OSError) as e:
                self.after(0, lambda m=str(e): self._abort(m, error=True))
                return
            if cancelled[0]:
                self.after(0, lambda: self._abort('Copie annulée.'))
                return
            self.after(0, lambda: self._progress_label.config(text='Calcul de la taille...'))
            self._run_copy(sources, info.root, cancelled)

        Thread(target=work, daemon=True).start()

    def _cancel(self):
        self._cancelled[0] = True
        self._cancel_btn.config(state=tk.DISABLED)
        self._log('Annulation en cours...')


class SDFormatFrame(ttk.Frame):
    """Onglet de formatage d'une carte SD en FAT32, clusters de 32 Ko."""

    def __init__(self, parent, config):
        super().__init__(parent)
        self.config = config
        self._drives = {}
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        dest = ttk.LabelFrame(self, text='Carte SD à formater')
        dest.grid(row=0, column=0, sticky=tk.EW, padx=10, pady=10)
        dest.columnconfigure(1, weight=1)

        ttk.Label(dest, text='Lecteur :').grid(row=0, column=0, sticky=tk.W, padx=(8, 4), pady=5)
        dc = ttk.Frame(dest)
        dc.grid(row=0, column=1, sticky=tk.W, pady=5)
        self._drive_combo = ttk.Combobox(dc, values=[_DRIVE_NONE], state='readonly', width=40)
        self._drive_combo.current(0)
        self._drive_combo.grid(row=0, column=0, padx=(0, 3))
        self._drive_combo.bind('<<ComboboxSelected>>', lambda e: self._update_status())
        ttk.Button(dc, text='↻', width=3, command=self._refresh).grid(row=0, column=1)

        ttk.Label(dest, text='Nom du volume :').grid(row=1, column=0, sticky=tk.W, padx=(8, 4), pady=5)
        self._label_var = tk.StringVar(value='3DS')
        ttk.Entry(dest, textvariable=self._label_var, width=16).grid(row=1, column=1, sticky=tk.W, pady=5)

        self._status = ttk.Label(dest, text='', foreground='grey')
        self._status.grid(row=2, column=0, columnspan=2, sticky=tk.W, padx=8, pady=(0, 5))

        self._note = ttk.Label(dest, foreground='grey', wraplength=560, justify=tk.LEFT)
        self._note.grid(row=3, column=0, columnspan=2, sticky=tk.W, padx=8, pady=(0, 8))
        self._update_note()

        prog = ttk.LabelFrame(self, text='Progression')
        prog.grid(row=2, column=0, sticky=tk.NSEW, padx=10, pady=(0, 10))
        prog.columnconfigure(0, weight=1)
        prog.rowconfigure(1, weight=1)
        self._progress = ttk.Progressbar(prog, mode='indeterminate')
        self._progress.grid(row=0, column=0, columnspan=2, sticky=tk.EW, padx=10, pady=(5, 0))
        scroll = ttk.Scrollbar(prog, orient=tk.VERTICAL)
        scroll.grid(row=1, column=1, sticky=tk.NS)
        self._log_text = tk.Text(prog, height=8, state=tk.DISABLED, wrap='word', yscrollcommand=scroll.set)
        self._log_text.grid(row=1, column=0, sticky=tk.NSEW, padx=(10, 0), pady=5)
        scroll.config(command=self._log_text.yview)

        self._start_btn = ttk.Button(self, text='Formater en FAT32 (32 Ko)', command=self._start)
        self._start_btn.grid(row=3, column=0, pady=10)

        if is_windows:
            self.after(200, self._refresh)
            self.winfo_toplevel().bind(DRIVE_LIMIT_EVENT, lambda e: (self._update_note(), self._refresh()), add='+')
        else:
            self._status.config(text='Formatage disponible uniquement sous Windows.')
            self._start_btn.config(state=tk.DISABLED)

    def _log(self, message):
        def _do():
            self._log_text.configure(state=tk.NORMAL)
            self._log_text.insert(tk.END, message + '\n')
            self._log_text.see(tk.END)
            self._log_text.configure(state=tk.DISABLED)
        self.after(0, _do)

    def _update_note(self):
        limit = sdformat.format_limit(sdformat.max_bytes_from_config(self.config))
        self._note.config(text='Format : FAT32 (y compris « Large FAT32 » au-delà de 32 Go), clusters de 32 Ko. '
                               f'C: et D: ne sont jamais proposés, ni les disques de plus de {limit} '
                               '(limite réglable dans Paramètres). '
                               'Windows demandera les droits administrateur.')

    def _refresh(self):
        self._status.config(text='Recherche des cartes SD...')

        def work():
            try:
                drives = sdformat.list_drives(sdformat.max_bytes_from_config(self.config))
            except Exception:
                drives = []

            def apply():
                self._drives = {d.letter: d for d in drives}
                values = [_DRIVE_NONE] + [d.display() for d in drives]
                cur = _drive_to_path(self._drive_combo.get())[:1]
                self._drive_combo.configure(values=values)
                idx = next((i for i, v in enumerate(values) if cur and _drive_to_path(v)[:1] == cur), 0)
                self._drive_combo.current(idx)
                self._update_status()
            try:
                self.after(0, apply)
            except RuntimeError:
                pass  # fenêtre fermée entre-temps
        Thread(target=work, daemon=True).start()

    def _update_status(self):
        letter = _drive_to_path(self._drive_combo.get())[:1]
        info = self._drives.get(letter)
        if not info:
            count = len(self._drives)
            self._status.config(text=f'{count} carte(s) SD détectée(s).' if count else
                                'Aucune carte SD détectée (insérez-la puis cliquez sur ↻).',
                                foreground='grey')
            return
        if info.raw:
            self._status.config(text=f'{letter}: n’est pas formatée (format non reconnu).', foreground='grey')
        elif info.fs.upper() == 'FAT32' and info.cluster == sdformat.CLUSTER_SIZE:
            self._status.config(text=f'{letter}: est déjà en FAT32, clusters de 32 Ko.', foreground='green')
        else:
            cl = f', clusters de {info.cluster // 1024} Ko' if info.cluster else ''
            self._status.config(text=f'Format actuel : {info.fs or "?"}{cl}', foreground='grey')
        if info.label:
            self._label_var.set(sdformat.sanitize_label(info.label) or '3DS')

    def _start(self):
        letter = _drive_to_path(self._drive_combo.get())[:1]
        if not letter:
            mb.showerror('Erreur', 'Veuillez sélectionner une carte SD.', parent=self)
            return
        label = sdformat.sanitize_label(self._label_var.get()) or '3DS'
        self._label_var.set(label)
        self._start_btn.config(state=tk.DISABLED)
        self._progress.start(15)

        def done(message=None, error=False):
            self._progress.stop()
            self._start_btn.config(state=tk.NORMAL)
            if message:
                (mb.showerror if error else mb.showinfo)('Formatage', message, parent=self)
            self._refresh()

        def check():
            # Re-vérifie le lecteur au moment du clic (la carte a pu être changée)
            try:
                info = sdformat.get_safe_drive(letter, sdformat.max_bytes_from_config(self.config))
            except sdformat.SDFormatError as e:
                self.after(0, lambda m=str(e): done(m, error=True))
                return
            self.after(0, lambda: confirm(info))

        def confirm(info):
            cl = f' ({info.cluster // 1024} Ko)' if info.cluster else ''
            if not mb.askyesno(
                    'Confirmer le formatage',
                    '⚠ TOUTES les données de la carte vont être définitivement effacées.\n\n'
                    f'Lecteur : {info.letter}:\n'
                    f'Nom actuel : {info.label or "(sans nom)"}\n'
                    f'Taille : {sdformat.human_size(info.size)}\n'
                    f'Format actuel : {"non reconnu" if info.raw else (info.fs or "?") + cl}\n\n'
                    f'Nouveau format : FAT32, clusters de 32 Ko, nom « {label} »\n\n'
                    f'Formater {info.letter}: ?',
                    icon=mb.WARNING, default=mb.NO, parent=self):
                done()
                return

            def work():
                try:
                    sdformat.format_drive_elevated(info.letter, label, info.size,
                                                   sdformat.max_bytes_from_config(self.config), log=self._log)
                except sdformat.SDFormatError as e:
                    self._log(f'Erreur : {e}')
                    self.after(0, lambda m=str(e): done(m, error=True))
                    return
                except Exception as e:
                    self._log(f'Erreur : {e}')
                    self.after(0, lambda m=f'{type(e).__name__} : {e}': done(m, error=True))
                    return
                self.after(0, lambda: done(f'{info.letter}: est formatée en FAT32 (clusters de 32 Ko).'))
            Thread(target=work, daemon=True).start()

        Thread(target=check, daemon=True).start()


class SettingsFrame(ttk.Frame):
    def __init__(self, parent, config):
        super().__init__(parent)
        self.config = config
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)  # row 1 = zone scrollable

        # ---- Section Profils (fixe, toujours visible) ----
        self._profiles = load_profiles()
        prof_sec = ttk.LabelFrame(self, text='Profils de configuration')
        prof_sec.grid(row=0, column=0, columnspan=2, sticky=tk.EW, padx=10, pady=(8, 4))
        prof_sec.columnconfigure(0, weight=1)

        self._profile_var = tk.StringVar()
        self._profile_combo = ttk.Combobox(prof_sec, textvariable=self._profile_var,
                                            state='readonly', width=32)
        self._profile_combo.grid(row=0, column=0, padx=(10, 5), pady=8, sticky=tk.W)

        ttk.Button(prof_sec, text='Charger', command=self._load_profile).grid(
            row=0, column=1, padx=3, pady=8)
        ttk.Button(prof_sec, text='Enregistrer sous...', command=self._save_profile_as).grid(
            row=0, column=2, padx=3, pady=8)
        ttk.Button(prof_sec, text='Supprimer', command=self._delete_profile).grid(
            row=0, column=3, padx=(3, 10), pady=8)

        self._refresh_profile_combo()

        # ---- Zone scrollable ----
        canvas = tk.Canvas(self, highlightthickness=0)
        vscroll = ttk.Scrollbar(self, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=vscroll.set)
        canvas.grid(row=1, column=0, sticky=tk.NSEW)
        vscroll.grid(row=1, column=1, sticky=tk.NS)

        body = ttk.Frame(canvas)
        body.columnconfigure(0, weight=1)
        cw = canvas.create_window((0, 0), window=body, anchor=tk.NW)

        body.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda e: canvas.itemconfig(cw, width=e.width))
        def _wheel(e):
            if canvas.yview() != (0.0, 1.0):  # rien à faire défiler si tout est visible
                canvas.yview_scroll(int(-1 * (e.delta / 120)), 'units')
        # Molette active uniquement quand la souris est au-dessus de la page Paramètres
        canvas.bind('<Enter>', lambda e: canvas.bind_all('<MouseWheel>', _wheel))
        canvas.bind('<Leave>', lambda e: canvas.unbind_all('<MouseWheel>'))

        _row = [0]  # mutable counter

        def next_row():
            r = _row[0]; _row[0] += 1; return r

        def make_picker(parent, var, pr, pc):
            """Ligne [entry][...][×] — chemin absolu (ex : source_root)."""
            ttk.Entry(parent, textvariable=var, width=30).grid(
                row=pr, column=pc, sticky=tk.EW, padx=5, pady=3)
            ttk.Button(parent, text='...', width=3,
                       command=lambda: self._browse(var)).grid(
                row=pr, column=pc + 1, padx=(0, 3), pady=3)
            ttk.Button(parent, text='×', width=2,
                       command=lambda: var.set('')).grid(
                row=pr, column=pc + 2, padx=(0, 8), pady=3)

        def make_rel_picker(parent, var, get_base, pr, pc):
            """Ligne [entry][...][×] — ouvre depuis get_base(), stocke le chemin relatif."""
            ttk.Entry(parent, textvariable=var, width=22).grid(
                row=pr, column=pc, sticky=tk.EW, padx=5, pady=3)
            def _browse():
                base = get_base()
                init = base if base and isdir(base) else None
                path = fd.askdirectory(parent=self, title='Sélectionner le dossier',
                                       mustexist=True, initialdir=init)
                if not path:
                    return
                if base and isdir(base):
                    try:
                        rel = relpath(path, base)
                        if not rel.startswith('..'):
                            var.set(rel)
                            return
                    except ValueError:
                        pass
                var.set(path)
            ttk.Button(parent, text='...', width=3, command=_browse).grid(
                row=pr, column=pc + 1, padx=(0, 3), pady=3)
            ttk.Button(parent, text='×', width=2,
                       command=lambda: var.set('')).grid(
                row=pr, column=pc + 2, padx=(0, 8), pady=3)

        def make_clear_entry(parent, var, get_base, pr, pc):
            """Ligne [entry][...][×] — sous-dossier relatif à get_base()."""
            ttk.Entry(parent, textvariable=var, width=25).grid(
                row=pr, column=pc, sticky=tk.EW, padx=5, pady=3)
            def _browse():
                base = get_base()
                init = base if base and isdir(base) else None
                path = fd.askdirectory(parent=self, title='Sélectionner le sous-dossier',
                                       mustexist=True, initialdir=init)
                if not path:
                    return
                if base and isdir(base):
                    try:
                        rel = relpath(path, base)
                        if not rel.startswith('..'):
                            var.set(rel)
                            return
                    except ValueError:
                        pass
                var.set(path)
            ttk.Button(parent, text='...', width=3, command=_browse).grid(
                row=pr, column=pc + 1, padx=(0, 3), pady=3)
            ttk.Button(parent, text='×', width=2,
                       command=lambda: var.set('')).grid(
                row=pr, column=pc + 2, padx=(0, 8), pady=3)

        # ---- Section Lecteurs ----
        drv_sec = ttk.LabelFrame(body, text='Lecteurs (cartes SD)')
        drv_sec.grid(row=next_row(), column=0, sticky=tk.EW, padx=10, pady=(10, 5))
        ttk.Label(drv_sec, text='Taille maximale des disques proposés :').grid(
            row=0, column=0, sticky=tk.W, padx=10, pady=(8, 3))
        limit_gb = sdformat.max_bytes_from_config(config) / 1000 ** 3
        self._max_size_var = tk.StringVar(value=f'{limit_gb:g}')
        ttk.Entry(drv_sec, textvariable=self._max_size_var, width=8).grid(row=0, column=1, sticky=tk.W, pady=(8, 3))
        ttk.Label(drv_sec, text='Go').grid(row=0, column=2, sticky=tk.W, padx=(4, 10), pady=(8, 3))
        ttk.Label(drv_sec, foreground='grey',
                  text='Les disques plus grands ne sont proposés dans aucun onglet (1100 Go = 1,1 To). '
                       'C: et D: ne sont jamais proposés.').grid(
            row=1, column=0, columnspan=3, sticky=tk.W, padx=10, pady=(0, 8))

        # ---- Section Dossiers source ----
        src_sec = ttk.LabelFrame(body, text='Dossiers source')
        src_sec.grid(row=next_row(), column=0, sticky=tk.EW, padx=10, pady=(10, 5))
        src_sec.columnconfigure(1, weight=1)

        ttk.Label(src_sec, text='Source commune :').grid(
            row=0, column=0, sticky=tk.W, padx=10, pady=(8, 3))
        self._source_var = tk.StringVar(value=config.get('source_root', ''))
        make_picker(src_sec, self._source_var, 0, 1)
        ttk.Label(src_sec, text='Utilisée par les packs CIA et NDS si "Source NDS" est vide.',
                  foreground='grey').grid(row=1, column=1, columnspan=3, sticky=tk.W, padx=5, pady=(0, 4))

        ttk.Label(src_sec, text='Source NDS (optionnel) :').grid(
            row=2, column=0, sticky=tk.W, padx=10, pady=3)
        self._nds_source_var = tk.StringVar(value=config.get('nds_source_root', ''))
        make_picker(src_sec, self._nds_source_var, 2, 1)
        ttk.Label(src_sec, text='Si rempli, remplace la source commune pour les packs NDS.',
                  foreground='grey').grid(row=3, column=1, columnspan=3, sticky=tk.W, padx=5, pady=(0, 8))

        # ---- Sections Pack NDS ----
        self._nds_pack_vars = {}
        nds_packs = config.get('nds_packs', {})

        for pack_name, pack_data in nds_packs.items():
            sec = ttk.LabelFrame(body, text=f'Pack NDS — {pack_name}')
            sec.grid(row=next_row(), column=0, sticky=tk.EW, padx=10, pady=5)
            sec.columnconfigure(1, weight=1)
            self._nds_pack_vars[pack_name] = {'languages': {}}

            ttk.Label(sec, text='Dossier du pack :').grid(
                row=0, column=0, sticky=tk.W, padx=10, pady=(8, 3))
            folder_var = tk.StringVar(value=pack_data.get('folder', ''))
            make_rel_picker(sec, folder_var,
                            lambda: (self._nds_source_var.get().strip()
                                     or self._source_var.get().strip()),
                            0, 1)
            self._nds_pack_vars[pack_name]['folder'] = folder_var

            ttk.Label(sec, text='Dossier de base :').grid(
                row=1, column=0, sticky=tk.W, padx=10, pady=3)
            base_var = tk.StringVar(value=pack_data.get('base_folder', 'Base NDS'))
            make_clear_entry(sec, base_var,
                             lambda fv=folder_var: join(
                                 self._nds_source_var.get().strip()
                                 or self._source_var.get().strip(),
                                 fv.get().strip()),
                             1, 1)
            self._nds_pack_vars[pack_name]['base_folder'] = base_var

            ttk.Separator(sec, orient=tk.HORIZONTAL).grid(
                row=2, column=0, columnspan=4, sticky=tk.EW, padx=10, pady=5)
            ttk.Label(sec, text='Dossiers par langue', font=('', 9, 'bold')).grid(
                row=3, column=0, columnspan=4, sticky=tk.W, padx=10, pady=(0, 3))

            for li, (lang, folder) in enumerate(pack_data.get('languages', {}).items()):
                ttk.Label(sec, text=f'{lang} :').grid(
                    row=4 + li, column=0, sticky=tk.W, padx=22, pady=2)
                lv = tk.StringVar(value=folder)
                make_clear_entry(sec, lv,
                                 lambda fv=folder_var: join(
                                     self._nds_source_var.get().strip()
                                     or self._source_var.get().strip(),
                                     fv.get().strip()),
                                 4 + li, 1)
                self._nds_pack_vars[pack_name]['languages'][lang] = lv

        # ---- Sections Pack CIA (une par taille, comme les packs NDS) ----
        cia_packs = config.get('cia_packs', {})
        pack_order = config.get('cia_pack_order', list(cia_packs.keys()))

        # Variantes (hors 'folder') dans l'ordre d'apparition du premier pack
        all_variants = []
        for sd in cia_packs.values():
            for k in sd:
                if k != 'folder' and k not in all_variants:
                    all_variants.append(k)

        self._cia_vars = {}
        for pack_name in pack_order:
            pack_data = cia_packs.get(pack_name, {})
            sec = ttk.LabelFrame(body, text=f'Pack CIA — {pack_name}')
            sec.grid(row=next_row(), column=0, sticky=tk.EW, padx=10, pady=5)
            sec.columnconfigure(1, weight=1)
            self._cia_vars[pack_name] = {}

            ttk.Label(sec, text='Dossier du pack :').grid(
                row=0, column=0, sticky=tk.W, padx=10, pady=(8, 3))
            folder_var = tk.StringVar(value=pack_data.get('folder', ''))
            make_rel_picker(sec, folder_var,
                            lambda: self._source_var.get().strip(),
                            0, 1)
            self._cia_vars[pack_name]['folder'] = folder_var

            ttk.Separator(sec, orient=tk.HORIZONTAL).grid(
                row=1, column=0, columnspan=4, sticky=tk.EW, padx=10, pady=5)
            ttk.Label(sec, text='Sous-dossiers par variante', font=('', 9, 'bold')).grid(
                row=2, column=0, columnspan=4, sticky=tk.W, padx=10, pady=(0, 3))

            for vi, variant in enumerate(all_variants):
                ttk.Label(sec, text=f'{variant} :').grid(
                    row=3 + vi, column=0, sticky=tk.W, padx=22, pady=2)
                var = tk.StringVar(value=pack_data.get(variant, ''))
                make_clear_entry(sec, var,
                                 lambda fv=folder_var: join(
                                     self._source_var.get().strip(),
                                     fv.get().strip()),
                                 3 + vi, 1)
                self._cia_vars[pack_name][variant] = var

        # Spacer bas
        ttk.Frame(body).grid(row=next_row(), column=0, pady=5)

        # ---- Bouton Enregistrer (hors zone scroll) ----
        bot = ttk.Frame(self)
        bot.grid(row=2, column=0, columnspan=2, pady=8)
        ttk.Button(bot, text='Enregistrer', command=self._save).grid(row=0, column=0, padx=10)
        self._status = ttk.Label(bot, text='', foreground='green')
        self._status.grid(row=0, column=1)

    def _browse(self, var):
        path = fd.askdirectory(parent=self, title='Sélectionner le dossier', mustexist=True)
        if path:
            var.set(path)

    def _refresh_profile_combo(self, select=None):
        names = sorted(self._profiles.keys())
        self._profile_combo.configure(values=names)
        if select and select in names:
            self._profile_var.set(select)
        elif names:
            self._profile_combo.current(0)
        else:
            self._profile_var.set('')

    def _load_profile(self):
        name = self._profile_var.get()
        if not name or name not in self._profiles:
            mb.showwarning('Profils', 'Veuillez sélectionner un profil.', parent=self)
            return
        data = self._profiles[name]
        if 'source_root' in data:
            self._source_var.set(data['source_root'])
        if 'nds_source_root' in data:
            self._nds_source_var.set(data['nds_source_root'])
        for pack_name, pack_vars in self._nds_pack_vars.items():
            pd = data.get('nds_packs', {}).get(pack_name, {})
            if 'folder' in pd:
                pack_vars['folder'].set(pd['folder'])
            if 'base_folder' in pd:
                pack_vars['base_folder'].set(pd['base_folder'])
            for lang, var in pack_vars['languages'].items():
                if lang in pd.get('languages', {}):
                    var.set(pd['languages'][lang])
        for size, variants in self._cia_vars.items():
            pd_cia = data.get('cia_packs', {}).get(size, {})
            for variant, var in variants.items():
                if variant in pd_cia:
                    var.set(pd_cia[variant])
        self._save()
        self._status.config(text=f'Profil "{name}" chargé.')
        self.after(3000, lambda: self._status.config(text=''))

    def _save_profile_as(self):
        import tkinter.simpledialog as sd_dlg
        name = sd_dlg.askstring('Enregistrer le profil', 'Nom du profil :', parent=self)
        if not name or not name.strip():
            return
        name = name.strip()
        data = {
            'source_root': self._source_var.get().strip(),
            'nds_source_root': self._nds_source_var.get().strip(),
            'nds_packs': {},
            'cia_packs': {},
        }
        for pack_name, pack_vars in self._nds_pack_vars.items():
            data['nds_packs'][pack_name] = {
                'folder': pack_vars['folder'].get().strip(),
                'base_folder': pack_vars['base_folder'].get().strip(),
                'languages': {lang: var.get().strip()
                              for lang, var in pack_vars['languages'].items()},
            }
        for size, variants in self._cia_vars.items():
            data['cia_packs'][size] = {variant: var.get().strip()
                                       for variant, var in variants.items()}
        self._profiles[name] = data
        save_profiles(self._profiles)
        self._refresh_profile_combo(select=name)
        self._status.config(text=f'Profil "{name}" enregistré.')
        self.after(3000, lambda: self._status.config(text=''))

    def _delete_profile(self):
        name = self._profile_var.get()
        if not name or name not in self._profiles:
            mb.showwarning('Profils', 'Veuillez sélectionner un profil.', parent=self)
            return
        if not mb.askyesno('Supprimer le profil', f'Supprimer le profil "{name}" ?', parent=self):
            return
        del self._profiles[name]
        save_profiles(self._profiles)
        self._refresh_profile_combo()
        self._status.config(text=f'Profil "{name}" supprimé.')
        self.after(3000, lambda: self._status.config(text=''))

    def _save(self):
        raw_limit = self._max_size_var.get().strip().replace(',', '.')
        try:
            limit_gb = float(raw_limit)
            if limit_gb <= 0:
                raise ValueError
        except ValueError:
            mb.showerror('Paramètres', 'La taille maximale des disques doit être un nombre de Go positif '
                                       '(ex. 1100).', parent=self)
            return
        limit_changed = limit_gb != sdformat.max_bytes_from_config(self.config) / 1000 ** 3
        self.config['max_drive_size_gb'] = limit_gb
        self.config['source_root'] = self._source_var.get().strip()
        self.config['nds_source_root'] = self._nds_source_var.get().strip()
        for pack_name, pack_vars in self._nds_pack_vars.items():
            if pack_name not in self.config['nds_packs']:
                self.config['nds_packs'][pack_name] = {}
            self.config['nds_packs'][pack_name]['folder'] = pack_vars['folder'].get().strip()
            self.config['nds_packs'][pack_name]['base_folder'] = pack_vars['base_folder'].get().strip()
            for lang, var in pack_vars['languages'].items():
                self.config['nds_packs'][pack_name]['languages'][lang] = var.get().strip()
        for size, variants in self._cia_vars.items():
            if size not in self.config['cia_packs']:
                self.config['cia_packs'][size] = {}
            for variant, var in variants.items():
                self.config['cia_packs'][size][variant] = var.get().strip()
        save_config(self.config)
        self._status.config(text='Enregistré.')
        self.after(3000, lambda: self._status.config(text=''))
        if limit_changed:
            # Recharge la liste des lecteurs dans tous les onglets
            self.winfo_toplevel().event_generate(DRIVE_LIMIT_EVENT)


def main():
    config = load_config()

    window = tk.Tk()
    window.title('3DS Hack Manager - ISA')

    if not (save3ds_fuse_path and isfile(save3ds_fuse_path)):
        mb.showwarning('Avertissement',
                       "save3ds_fuse est introuvable.\n"
                       "L'installation de fichiers CIA ne sera pas disponible.")

    notebook = ttk.Notebook(window)
    notebook.pack(fill=tk.BOTH, expand=True)

    install_tab = CustomInstallGUI(notebook, config)
    notebook.add(install_tab, text='Installation CIA')

    nds_tab = NDSCopyFrame(notebook, config)
    notebook.add(nds_tab, text='Copie Pack NDS')

    custom_tab = CustomPackFrame(notebook, config)
    notebook.add(custom_tab, text='Packs Customs')

    format_tab = SDFormatFrame(notebook, config)
    notebook.add(format_tab, text='Formater SD')

    settings_tab = SettingsFrame(notebook, config)
    notebook.add(settings_tab, text='Paramètres')

    window.mainloop()


if __name__ == '__main__':
    main()
