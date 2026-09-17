# This file is a part of custom-install.
#
# This file is licensed under The MIT License (MIT).
# You can find the full license text in LICENSE.md in the root of this project.

"""Formatage de carte SD en FAT32 (clusters 32 Ko) et garde-fous sur les lecteurs.

Windows refuse de formater en FAT32 au-delà de 32 Go (format.com / Format-Volume).
Ce module écrit directement les structures FAT32 sur le volume (même principe
que fat32format de Ridgecrop), ce qui fonctionne pour toutes les tailles de SD.

Utilisable en ligne de commande (c'est ainsi que l'interface le lance en mode
administrateur) :
    python -m custominstall.sdformat format --drive E --label 3DS --expect-size 31914983424
"""

import json
import os
import shutil
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from os.path import abspath, dirname

is_windows = sys.platform == 'win32'

CLUSTER_SIZE = 32 * 1024                 # 32 Ko, format attendu par la 3DS
FORBIDDEN_LETTERS = {'C', 'D'}           # jamais proposés, quoi qu'il arrive
DEFAULT_MAX_SIZE_GB = 1100               # 1,1 To, réglable dans Paramètres (max_drive_size_gb)
KEEP_ON_CLEAR = {'system volume information', '$recycle.bin'}

ERROR_NOT_READY = 21                     # lecteur de cartes sans carte
IOCTL_STORAGE_MEDIA_REMOVAL = 0x002D4804
IOCTL_STORAGE_EJECT_MEDIA = 0x002D4808

_APP_ROOT = dirname(dirname(abspath(__file__)))


class SDFormatError(Exception):
    pass


def max_bytes_from_config(config):
    """Limite de taille des disques proposés, en octets (Go décimaux, comme les SD)."""
    try:
        gb = float(str((config or {}).get('max_drive_size_gb', DEFAULT_MAX_SIZE_GB)).replace(',', '.'))
    except ValueError:
        gb = DEFAULT_MAX_SIZE_GB
    if gb <= 0:
        gb = DEFAULT_MAX_SIZE_GB
    return int(gb * 1000 ** 3)


def format_limit(max_bytes):
    gb = max_bytes / 1000 ** 3
    text = f'{gb:g} Go' if gb < 1000 else f'{gb / 1000:g} To'
    return text.replace('.', ',')


# --------------------------------------------------------------------------- #
# Inventaire des lecteurs

@dataclass
class DriveInfo:
    letter: str
    label: str = ''
    fs: str = ''
    cluster: int = 0
    size: int = 0          # taille du volume (0 = inconnue)
    safe: bool = False
    reason: str = ''

    @property
    def root(self):
        return self.letter + ':\\'

    @property
    def raw(self):
        return self.fs == 'RAW'

    def display(self):
        parts = []
        if self.label:
            parts.append(self.label)
        if self.size:
            parts.append(human_size(self.size))
        if self.raw:
            parts.append('non formatée')
        elif self.fs:
            fs = self.fs
            if self.cluster:
                fs += f' {self.cluster // 1024} Ko'
            parts.append(fs)
        return f'{self.letter}: — ' + ', '.join(parts) if parts else f'{self.letter}:'


def human_size(n):
    if n >= 1024 ** 3:
        return f'{n / 1024 ** 3:.1f} Go'
    return f'{n / 1024 ** 2:.0f} Mo'


def _volume_info(letter):
    """Système de fichiers, nom, taille de cluster et taille d'un volume (ctypes, rapide).
    None si le lecteur ne contient pas de carte ; fs = 'RAW' si la carte n'est pas lisible
    (jamais formatée ou système de fichiers abîmé : elle doit rester formatable)."""
    import ctypes as ct
    import ctypes.wintypes as wt
    k32 = ct.WinDLL('kernel32', use_last_error=True)
    root = letter + ':\\'
    name = ct.create_unicode_buffer(261)
    fs = ct.create_unicode_buffer(261)
    old_mode = k32.SetErrorMode(0x0001)  # pas de fenêtre « Insérez un disque » sur un lecteur vide
    try:
        if not k32.GetVolumeInformationW(root, name, 261, None, None, None, fs, 261):
            if ct.get_last_error() == ERROR_NOT_READY:
                return None
            return {'label': '', 'fs': 'RAW', 'cluster': 0, 'size': 0}
        info = {'label': name.value.strip(), 'fs': fs.value.strip(), 'cluster': 0, 'size': 0}
        spc, bps, free_c, total_c = wt.DWORD(), wt.DWORD(), wt.DWORD(), wt.DWORD()
        if k32.GetDiskFreeSpaceW(root, ct.byref(spc), ct.byref(bps), ct.byref(free_c), ct.byref(total_c)):
            info['cluster'] = spc.value * bps.value
        total = ct.c_ulonglong(0)
        if k32.GetDiskFreeSpaceExW(root, None, ct.byref(total), None):
            info['size'] = total.value
        return info
    finally:
        k32.SetErrorMode(old_mode)


_PS_DISKS = r"""
$ErrorActionPreference = 'SilentlyContinue'
$out = @()
foreach ($p in Get-Partition) {
  $l = [string]$p.DriveLetter
  if ($l -notmatch '^[A-Za-z]$') { continue }
  $d = Get-Disk -Number $p.DiskNumber
  $out += [pscustomobject]@{
    Letter = $l.ToUpper(); PartitionStyle = [string]$d.PartitionStyle;
    PartitionSize = [int64]$p.Size; MbrType = $p.MbrType
  }
}
ConvertTo-Json -Compress -InputObject @($out)
"""


def _powershell(script, timeout=30):
    flags = 0x08000000 if is_windows else 0  # CREATE_NO_WINDOW : pas de console qui clignote
    return subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
                          capture_output=True, text=True, timeout=timeout, creationflags=flags)


def _partition_map():
    """{lettre: infos de partition} via PowerShell. Sert uniquement pour les cartes non formatées
    (taille) et pour corriger le type de partition après formatage. {} si indisponible."""
    data = []
    for attempt in range(2):  # le premier lancement de PowerShell est parfois lent / vide
        try:
            res = _powershell(_PS_DISKS)
            data = json.loads(res.stdout or '[]')
        except (OSError, ValueError, subprocess.SubprocessError):
            data = []
        if data:
            break
    if isinstance(data, dict):
        data = [data]
    return {str(d.get('Letter', '')).upper(): d for d in data if d.get('Letter')}


def _dos_device(letter):
    r"""Cible Windows d'une lettre : « \Device\HarddiskVolume3 », ou « \??\D:\dossier » pour un subst."""
    import ctypes as ct
    buf = ct.create_unicode_buffer(1024)
    if not ct.windll.kernel32.QueryDosDeviceW(letter + ':', buf, 1024):
        return ''
    return buf.value


def evaluate_drive(letter, vol, max_bytes, forbidden_devices=()):
    """Construit un DriveInfo : seuls C:, D: (et leurs alias) et les disques au-delà de la limite sont exclus."""
    info = DriveInfo(letter=letter, label=vol.get('label', ''), fs=vol.get('fs', ''),
                     cluster=vol.get('cluster', 0), size=vol.get('size', 0))
    device = vol.get('device', '')
    if letter in FORBIDDEN_LETTERS:
        info.reason = 'C: et D: ne sont jamais proposés'
    elif device.startswith('\\??\\'):
        # Lettre créée avec subst : l'accès direct viserait le vrai disque (souvent C: ou D:)
        info.reason = f'lecteur virtuel vers {device[4:] or "un autre disque"}'
    elif device and device in forbidden_devices:
        info.reason = 'autre lettre de C: ou D:'
    elif not info.size:
        info.reason = 'taille inconnue'
    elif info.size > max_bytes:
        info.reason = f'plus de {format_limit(max_bytes)}'
    else:
        info.safe = True
    return info


def list_drives(max_bytes, include_unsafe=False):
    """Liste les lecteurs contenant une carte ; par défaut uniquement ceux autorisés."""
    if not is_windows:
        return []
    import ctypes as ct
    bitmask = ct.windll.kernel32.GetLogicalDrives()
    letters = [chr(65 + i) for i in range(26) if bitmask & (1 << i)]
    partitions = None
    forbidden_devices = {_dos_device(l) for l in FORBIDDEN_LETTERS} - {''}
    drives = []
    for letter in letters:
        if letter in ('A', 'B'):
            continue
        vol = _volume_info(letter)
        if vol is None:
            continue
        if vol['fs'] == 'RAW' and not vol['size']:
            if partitions is None:
                partitions = _partition_map()
            vol['size'] = int((partitions.get(letter) or {}).get('PartitionSize') or 0)
        vol['device'] = _dos_device(letter)
        info = evaluate_drive(letter, vol, max_bytes, forbidden_devices)
        if info.safe or include_unsafe:
            drives.append(info)
    return drives


def get_safe_drive(letter, max_bytes):
    """DriveInfo si le lecteur est autorisé, sinon lève SDFormatError avec la raison."""
    letter = (letter or '').strip()[:1].upper()
    if not letter:
        raise SDFormatError('Aucun lecteur sélectionné.')
    for info in list_drives(max_bytes, include_unsafe=True):
        if info.letter == letter:
            if not info.safe:
                raise SDFormatError(f'Le lecteur {letter}: est exclu ({info.reason}).')
            return info
    raise SDFormatError(f'Le lecteur {letter}: est introuvable ou ne contient pas de carte.')


def check_sd_format(letter):
    """(ok, fs, cluster) : la SD doit être en FAT32 avec des clusters de 32 Ko."""
    vol = _volume_info(letter.strip()[:1].upper()) if is_windows else None
    if not vol:
        return False, '', 0
    fs, cluster = vol.get('fs', ''), vol.get('cluster', 0)
    return fs.upper() == 'FAT32' and cluster == CLUSTER_SIZE, fs, cluster


def clear_drive_contents(root, max_bytes, log=print, cancelled=None):
    """Supprime tout le contenu à la racine d'une SD (sauf dossiers système Windows)."""
    root = os.path.abspath(root)
    if not (len(root) == 3 and root[1:] == ':\\'):
        raise SDFormatError(f'Refus de vider « {root} » : ce n’est pas la racine d’un lecteur.')
    get_safe_drive(root[0], max_bytes)  # re-vérifie les exclusions juste avant de supprimer

    def _on_error(func, path, exc_info):
        # Fichiers en lecture seule : on retire l'attribut et on réessaie
        try:
            os.chmod(path, 0o666)
            func(path)
        except OSError as e:
            log(f'  Impossible de supprimer {path} : {e}')

    count = 0
    for entry in list(os.scandir(root)):
        if cancelled and cancelled[0]:
            return count
        if entry.name.lower() in KEEP_ON_CLEAR:
            continue
        log(f'  Suppression : {entry.name}')
        try:
            if entry.is_dir(follow_symlinks=False):
                if sys.version_info >= (3, 12):
                    shutil.rmtree(entry.path, onexc=lambda f, p, e: _on_error(f, p, None))
                else:
                    shutil.rmtree(entry.path, onerror=_on_error)
            else:
                try:
                    os.remove(entry.path)
                except PermissionError:
                    os.chmod(entry.path, 0o666)
                    os.remove(entry.path)
            count += 1
        except OSError as e:
            log(f'  Impossible de supprimer {entry.name} : {e}')
    return count


# --------------------------------------------------------------------------- #
# Éjection

def _media_present(letter):
    import ctypes as ct
    if not ct.windll.kernel32.GetLogicalDrives() & (1 << (ord(letter) - 65)):
        return False
    return _volume_info(letter) is not None


def _eject_volume(letter):
    """Verrouille, démonte et éjecte le média. None si réussi, sinon 'in_use', 'open' ou 'eject'."""
    import ctypes as ct
    import ctypes.wintypes as wt
    k32 = ct.WinDLL('kernel32', use_last_error=True)
    k32.CreateFileW.restype = wt.HANDLE
    k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ct.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE]
    k32.DeviceIoControl.argtypes = [wt.HANDLE, wt.DWORD, ct.c_void_p, wt.DWORD, ct.c_void_p, wt.DWORD,
                                    ct.POINTER(wt.DWORD), ct.c_void_p]
    k32.FlushFileBuffers.argtypes = [wt.HANDLE]
    k32.CloseHandle.argtypes = [wt.HANDLE]
    invalid = (None, ct.c_void_p(-1).value)

    path = '\\\\.\\' + letter + ':'
    handle = k32.CreateFileW(path, 0xC0000000, 0x3, None, 3, 0, None)
    if handle in invalid:
        handle = k32.CreateFileW(path, 0x80000000, 0x3, None, 3, 0, None)
    if handle in invalid:
        return 'open'

    def ioctl(code, inbuf=None, insize=0):
        returned = wt.DWORD(0)
        return bool(k32.DeviceIoControl(handle, code, inbuf, insize, None, 0, ct.byref(returned), None))

    try:
        k32.FlushFileBuffers(handle)  # écrit ce qui reste en cache avant de retirer la carte
        for attempt in range(10):
            if ioctl(VolumeTarget.FSCTL_LOCK_VOLUME):
                break
            time.sleep(0.3)
        else:
            return 'in_use'  # un fichier ou un dossier de la carte est ouvert
        ioctl(VolumeTarget.FSCTL_DISMOUNT_VOLUME)
        allow = ct.create_string_buffer(b'\x00', 1)  # PREVENT_MEDIA_REMOVAL = FALSE
        ioctl(IOCTL_STORAGE_MEDIA_REMOVAL, allow, 1)
        if not ioctl(IOCTL_STORAGE_EJECT_MEDIA):
            return 'eject'
        return None
    finally:
        k32.CloseHandle(handle)  # libère aussi le verrou


def eject_drive(letter, max_bytes, log=print):
    """Éjecte la carte SD (comme « Éjecter » dans l'Explorateur). Lève SDFormatError en cas d'échec."""
    if not is_windows:
        raise SDFormatError('L’éjection n’est disponible que sous Windows.')
    info = get_safe_drive(letter, max_bytes)
    letter = info.letter
    log(f'Éjection de {letter}:...')
    error = _eject_volume(letter)
    if error == 'in_use':
        raise SDFormatError(f'Impossible d’éjecter {letter}: : un fichier ou un dossier de la carte est ouvert '
                            '(fenêtre de l’Explorateur, autre programme). Fermez-le puis réessayez.')
    if error:
        # Lecteur qui refuse l'éjection directe : on passe par la commande « Éjecter » de l'Explorateur
        _powershell("(New-Object -ComObject Shell.Application).Namespace(17)"
                    f".ParseName('{letter}:\\').InvokeVerb('Eject')")
    for _ in range(40):
        if not _media_present(letter):
            log(f'{letter}: éjectée, la carte peut être retirée.')
            return True
        time.sleep(0.25)
    raise SDFormatError(f'Windows n’a pas pu éjecter {letter}:. '
                        'Utilisez « Retirer le périphérique en toute sécurité » dans la barre des tâches.')


# --------------------------------------------------------------------------- #
# Écriture des structures FAT32

def sanitize_label(label):
    allowed = set('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 !#$%&\'()-@^_`{}~')
    label = ''.join(c if c in allowed else '_' for c in (label or '').upper())
    return label.strip()[:11].rstrip()


@dataclass
class Fat32Layout:
    bytes_per_sector: int
    sectors_per_cluster: int
    total_sectors: int
    reserved: int
    num_fats: int
    fat_size: int
    cluster_count: int

    @property
    def data_start(self):
        return self.reserved + self.num_fats * self.fat_size


def compute_layout(volume_bytes, bytes_per_sector=512, cluster_size=CLUSTER_SIZE):
    if bytes_per_sector not in (512, 1024, 2048, 4096) or cluster_size % bytes_per_sector:
        raise SDFormatError(f'Taille de secteur non supportée : {bytes_per_sector} octets.')
    spc = cluster_size // bytes_per_sector
    total = volume_bytes // bytes_per_sector
    if total > 0xFFFFFFFF:
        raise SDFormatError('Volume trop grand pour FAT32 (plus de 2^32 secteurs).')
    reserved, num_fats = 32, 2
    # Formule de fat32format (Ridgecrop) : taille d'une FAT en secteurs
    fat_size = (4 * (total - reserved)) // (spc * bytes_per_sector + 4 * num_fats) + 1
    clusters = (total - reserved - num_fats * fat_size) // spc
    if clusters < 65525:
        raise SDFormatError('Carte trop petite pour du FAT32 en clusters de 32 Ko (minimum ≈ 2 Go).')
    if clusters > 0x0FFFFFF5:
        raise SDFormatError('Trop de clusters pour FAT32.')
    if fat_size * bytes_per_sector // 4 < clusters + 2:
        raise SDFormatError('Erreur interne : FAT trop petite.')
    return Fat32Layout(bytes_per_sector, spc, total, reserved, num_fats, fat_size, clusters)


def _dos_datetime(t):
    lt = time.localtime(t)
    dos_time = (lt.tm_hour << 11) | (lt.tm_min << 5) | (lt.tm_sec // 2)
    dos_date = (max(lt.tm_year - 1980, 0) << 9) | (lt.tm_mon << 5) | lt.tm_mday
    return dos_time, dos_date


def build_sectors(layout, label, hidden_sectors=0, sectors_per_track=63, heads=255, now=None):
    """Retourne {n° de secteur: bytes} des secteurs non nuls à écrire."""
    bps = layout.bytes_per_sector
    now = time.time() if now is None else now
    label = sanitize_label(label)
    vol_label = (label or 'NO NAME').ljust(11).encode('ascii')
    lt = time.localtime(now)
    vol_id = ((lt.tm_sec + lt.tm_mon * 256 + lt.tm_mday) & 0xFFFF) << 16 | \
             ((lt.tm_hour * 256 + lt.tm_min + lt.tm_year) & 0xFFFF)

    boot = bytearray(bps)
    struct.pack_into('<3s8sHBHBHHBHHHLLLHHLHH12sBBBL11s8s', boot, 0,
                     b'\xEB\x58\x90', b'MSWIN4.1', bps, layout.sectors_per_cluster,
                     layout.reserved, layout.num_fats, 0, 0, 0xF8, 0,
                     min(sectors_per_track, 0xFFFF) or 63, min(heads, 0xFFFF) or 255,
                     hidden_sectors & 0xFFFFFFFF, layout.total_sectors, layout.fat_size,
                     0, 0, 2, 1, 6, b'\0' * 12, 0x80, 0, 0x29, vol_id, vol_label, b'FAT32   ')
    boot[510:512] = b'\x55\xAA'
    if bps > 512:
        boot[bps - 2:bps] = b'\x55\xAA'

    fsinfo = bytearray(bps)
    struct.pack_into('<L', fsinfo, 0, 0x41615252)
    struct.pack_into('<LLL', fsinfo, 484, 0x61417272, layout.cluster_count - 1, 3)
    struct.pack_into('<L', fsinfo, 508, 0xAA550000)

    third = bytearray(bps)
    third[510:512] = b'\x55\xAA'

    fat = bytearray(bps)
    struct.pack_into('<LLL', fat, 0, 0x0FFFFFF8, 0x0FFFFFFF, 0x0FFFFFFF)

    sectors = {}
    for base in (0, 6):  # secteur de boot + copie de secours
        sectors[base] = bytes(boot)
        sectors[base + 1] = bytes(fsinfo)
        sectors[base + 2] = bytes(third)
    for i in range(layout.num_fats):
        sectors[layout.reserved + i * layout.fat_size] = bytes(fat)

    if label:
        root = bytearray(bps)
        dos_time, dos_date = _dos_datetime(now)
        struct.pack_into('<11sBBBHHHHHHHL', root, 0, vol_label, 0x08, 0, 0,
                         dos_time, dos_date, dos_date, 0, dos_time, dos_date, 0, 0)
        sectors[layout.data_start] = bytes(root)
    return sectors


def write_fat32(target, label, log=print):
    """Formate `target` (volume Windows ou image fichier) en FAT32 32 Ko."""
    layout = compute_layout(target.size_bytes, target.bytes_per_sector)
    bps = layout.bytes_per_sector
    log(f'Secteurs : {layout.total_sectors} x {bps} o — clusters : {layout.cluster_count} x '
        f'{layout.sectors_per_cluster * bps // 1024} Ko — FAT : {layout.fat_size} secteurs x2')

    # 1. Remise à zéro de la zone système (réservés + FATs + 1er cluster du dossier racine)
    system_area = layout.data_start + layout.sectors_per_cluster
    chunk = max(1, (4 * 1024 * 1024) // bps)
    zero = bytes(chunk * bps)
    done = 0
    while done < system_area:
        n = min(chunk, system_area - done)
        target.write_sectors(done, zero[:n * bps])
        done += n
    # Efface aussi la fin du volume (secteur de secours NTFS/exFAT éventuel)
    tail = 8
    if layout.total_sectors > system_area + tail:
        target.write_sectors(layout.total_sectors - tail, bytes(tail * bps))

    # 2. Structures FAT32
    for sector, data in sorted(build_sectors(layout, label, target.hidden_sectors,
                                             target.sectors_per_track, target.heads).items()):
        target.write_sectors(sector, data)
    target.flush()
    return layout


class ImageTarget:
    """Fichier image (tests) : se comporte comme un volume brut."""

    def __init__(self, path, size_bytes=None, bytes_per_sector=512):
        self.path = path
        self.bytes_per_sector = bytes_per_sector
        if size_bytes is not None:
            with open(path, 'wb') as f:
                f.truncate(size_bytes)
        self.size_bytes = os.path.getsize(path)
        self.hidden_sectors, self.sectors_per_track, self.heads = 0, 63, 255
        self._f = open(path, 'r+b')

    def write_sectors(self, sector, data):
        assert len(data) % self.bytes_per_sector == 0
        self._f.seek(sector * self.bytes_per_sector)
        self._f.write(data)

    def flush(self):
        self._f.flush()

    def close(self):
        self._f.close()


class VolumeTarget:
    """Volume Windows ouvert en accès brut (\\\\.\\E:), verrouillé et démonté."""

    FSCTL_LOCK_VOLUME = 0x00090018
    FSCTL_UNLOCK_VOLUME = 0x0009001C
    FSCTL_DISMOUNT_VOLUME = 0x00090020
    FSCTL_ALLOW_EXTENDED_DASD_IO = 0x00090083
    IOCTL_DISK_GET_DRIVE_GEOMETRY = 0x00070000
    IOCTL_DISK_GET_PARTITION_INFO_EX = 0x00070048
    IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C

    def __init__(self, letter, log=print):
        import ctypes as ct
        import ctypes.wintypes as wt
        self._ct, self._wt = ct, wt
        k32 = ct.WinDLL('kernel32', use_last_error=True)
        k32.CreateFileW.restype = wt.HANDLE
        k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ct.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE]
        k32.DeviceIoControl.argtypes = [wt.HANDLE, wt.DWORD, ct.c_void_p, wt.DWORD, ct.c_void_p, wt.DWORD,
                                        ct.POINTER(wt.DWORD), ct.c_void_p]
        k32.SetFilePointerEx.argtypes = [wt.HANDLE, ct.c_longlong, ct.POINTER(ct.c_longlong), wt.DWORD]
        k32.WriteFile.argtypes = [wt.HANDLE, ct.c_void_p, wt.DWORD, ct.POINTER(wt.DWORD), ct.c_void_p]
        k32.FlushFileBuffers.argtypes = [wt.HANDLE]
        k32.CloseHandle.argtypes = [wt.HANDLE]
        self._k32 = k32
        self.letter = letter
        self._locked = False

        handle = k32.CreateFileW(f'\\\\.\\{letter}:', 0xC0000000, 0x3, None, 3, 0, None)
        if handle in (None, ct.c_void_p(-1).value):
            err = ct.get_last_error()
            if err == 5:
                raise SDFormatError('Accès refusé : les droits administrateur sont nécessaires.')
            raise SDFormatError(f'Impossible d’ouvrir le volume {letter}: (erreur Windows {err}).')
        self._h = handle

        try:
            # Verrouillage : échoue si un programme (Explorateur...) a un fichier ouvert
            for attempt in range(10):
                if self._ioctl(self.FSCTL_LOCK_VOLUME):
                    self._locked = True
                    break
                time.sleep(0.5)
            if not self._locked:
                log('Volume utilisé par un autre programme : démontage forcé.')
            self._ioctl(self.FSCTL_DISMOUNT_VOLUME)
            if not self._locked:
                self._locked = self._ioctl(self.FSCTL_LOCK_VOLUME)
            self._ioctl(self.FSCTL_ALLOW_EXTENDED_DASD_IO)

            geo = ct.create_string_buffer(24)
            if not self._ioctl(self.IOCTL_DISK_GET_DRIVE_GEOMETRY, out=geo):
                raise SDFormatError('Impossible de lire la géométrie du disque.')
            _cyl, _media, self.heads, self.sectors_per_track, self.bytes_per_sector = \
                struct.unpack('<qLLLL', geo.raw)

            length = ct.create_string_buffer(8)
            if not self._ioctl(self.IOCTL_DISK_GET_LENGTH_INFO, out=length):
                raise SDFormatError('Impossible de lire la taille du volume.')
            self.size_bytes = struct.unpack('<q', length.raw)[0]

            part = ct.create_string_buffer(144)
            if self._ioctl(self.IOCTL_DISK_GET_PARTITION_INFO_EX, out=part):
                offset = struct.unpack_from('<q', part.raw, 8)[0]
                self.hidden_sectors = offset // self.bytes_per_sector
            else:
                self.hidden_sectors = 0
        except Exception:
            self.close()
            raise

    def _ioctl(self, code, out=None):
        ct, wt = self._ct, self._wt
        returned = wt.DWORD(0)
        return bool(self._k32.DeviceIoControl(self._h, code, None, 0,
                                              out, len(out) if out is not None else 0,
                                              ct.byref(returned), None))

    def write_sectors(self, sector, data):
        ct, wt = self._ct, self._wt
        if len(data) % self.bytes_per_sector:
            raise SDFormatError('Écriture non alignée sur les secteurs.')
        if not self._k32.SetFilePointerEx(self._h, sector * self.bytes_per_sector, None, 0):
            raise SDFormatError(f'Positionnement impossible (erreur {ct.get_last_error()}).')
        buf = ct.create_string_buffer(bytes(data), len(data))
        written = wt.DWORD(0)
        if not self._k32.WriteFile(self._h, buf, len(data), ct.byref(written), None) \
                or written.value != len(data):
            raise SDFormatError(f'Écriture impossible au secteur {sector} (erreur {ct.get_last_error()}).')

    def flush(self):
        self._k32.FlushFileBuffers(self._h)

    def close(self):
        if getattr(self, '_h', None):
            if self._locked:
                self._ioctl(self.FSCTL_UNLOCK_VOLUME)
                self._locked = False
            self._k32.CloseHandle(self._h)
            self._h = None


# --------------------------------------------------------------------------- #
# Formatage d'un lecteur (à exécuter en administrateur)

def is_admin():
    if not is_windows:
        return False
    import ctypes as ct
    try:
        return bool(ct.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def format_drive(letter, label='3DS', expect_size=None, max_bytes=None, log=print):
    """Formate un lecteur SD en FAT32 32 Ko après avoir re-vérifié les exclusions."""
    if not is_windows:
        raise SDFormatError('Le formatage n’est disponible que sous Windows.')
    max_bytes = max_bytes or max_bytes_from_config(None)
    info = get_safe_drive(letter, max_bytes)
    letter = info.letter
    if expect_size and info.size and abs(int(expect_size) - info.size) > CLUSTER_SIZE * 64:
        raise SDFormatError('La carte présente dans le lecteur a changé depuis la confirmation. Abandon.')

    log(f'Formatage de {letter}: ({human_size(info.size)}, {"non formatée" if info.raw else info.fs or "?"}) '
        'en FAT32 32 Ko...')
    target = VolumeTarget(letter, log=log)
    try:
        if target.size_bytes > max_bytes:
            raise SDFormatError(f'Volume de plus de {format_limit(max_bytes)} : abandon.')
        write_fat32(target, label, log=log)
    finally:
        target.close()

    disk = _partition_map().get(letter, {})
    if str(disk.get('PartitionStyle', '')).upper() == 'MBR' and disk.get('MbrType') not in (11, 12):
        # Type de partition 0x0C (FAT32 LBA) pour que tous les lecteurs la reconnaissent
        _powershell(f'Set-Partition -DriveLetter {letter} -MbrType 12')

    # Windows remonte le volume : on attend et on vérifie le résultat
    for _ in range(40):
        ok, fs, cluster = check_sd_format(letter)
        if ok:
            log(f'Terminé : {letter}: est en FAT32, clusters de 32 Ko.')
            return True
        time.sleep(0.5)
    ok, fs, cluster = check_sd_format(letter)
    raise SDFormatError(f'Formatage écrit mais le volume est vu comme « {fs or "illisible"} » '
                        f'({cluster // 1024 if cluster else "?"} Ko). Retirez et réinsérez la carte.')


def format_drive_elevated(letter, label, expect_size, max_bytes, log=print):
    """Lance le formatage dans un processus administrateur (invite UAC) et attend la fin."""
    if is_admin():
        return format_drive(letter, label, expect_size, max_bytes, log)
    if getattr(sys, 'frozen', False):
        raise SDFormatError('Relancez l’application en tant qu’administrateur pour formater.')

    import ctypes as ct
    import ctypes.wintypes as wt
    import tempfile

    fd, result_path = tempfile.mkstemp(prefix='sdformat-', suffix='.json')
    os.close(fd)
    params = subprocess.list2cmdline(['-m', 'custominstall.sdformat', 'format', '--drive', letter,
                                      '--label', label, '--expect-size', str(expect_size or 0),
                                      '--max-bytes', str(max_bytes),
                                      '--result', result_path])

    class SHELLEXECUTEINFOW(ct.Structure):
        _fields_ = [('cbSize', wt.DWORD), ('fMask', ct.c_ulong), ('hwnd', wt.HWND),
                    ('lpVerb', wt.LPCWSTR), ('lpFile', wt.LPCWSTR), ('lpParameters', wt.LPCWSTR),
                    ('lpDirectory', wt.LPCWSTR), ('nShow', ct.c_int), ('hInstApp', wt.HINSTANCE),
                    ('lpIDList', ct.c_void_p), ('lpClass', wt.LPCWSTR), ('hkeyClass', wt.HKEY),
                    ('dwHotKey', wt.DWORD), ('hIconOrMonitor', wt.HANDLE), ('hProcess', wt.HANDLE)]

    sei = SHELLEXECUTEINFOW()
    sei.cbSize = ct.sizeof(sei)
    sei.fMask = 0x00000040  # SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb = 'runas'
    sei.lpFile = sys.executable
    sei.lpParameters = params
    sei.lpDirectory = _APP_ROOT
    sei.nShow = 0  # SW_HIDE
    log('Demande des droits administrateur (fenêtre Windows)...')
    try:
        shell32 = ct.WinDLL('shell32', use_last_error=True)
        if not shell32.ShellExecuteExW(ct.byref(sei)):
            err = ct.get_last_error()
            if err == 1223:
                raise SDFormatError('Droits administrateur refusés : formatage annulé.')
            raise SDFormatError(f'Impossible de lancer le formatage (erreur {err}).')
        ct.windll.kernel32.WaitForSingleObject(sei.hProcess, 0xFFFFFFFF)
        ct.windll.kernel32.CloseHandle(sei.hProcess)
        try:
            with open(result_path, 'r', encoding='utf-8') as f:
                result = json.load(f)
        except (OSError, ValueError):
            raise SDFormatError('Le processus de formatage s’est arrêté sans résultat.')
    finally:
        try:
            os.remove(result_path)
        except OSError:
            pass
    for line in result.get('log', []):
        log(line)
    if not result.get('ok'):
        raise SDFormatError(result.get('error') or 'Échec du formatage.')
    return True


def _cli(argv=None):
    import argparse
    parser = argparse.ArgumentParser(prog='python -m custominstall.sdformat')
    sub = parser.add_subparsers(dest='cmd', required=True)
    lst = sub.add_parser('list', help='liste les lecteurs et indique ceux qui sont exclus')
    lst.add_argument('--max-bytes', type=int, default=0)
    fmt = sub.add_parser('format', help='formate une SD en FAT32 32 Ko (administrateur)')
    fmt.add_argument('--drive', required=True)
    fmt.add_argument('--label', default='3DS')
    fmt.add_argument('--expect-size', type=int, default=0)
    fmt.add_argument('--max-bytes', type=int, default=0)
    fmt.add_argument('--result', help='fichier JSON où écrire le résultat')
    args = parser.parse_args(argv)
    max_bytes = args.max_bytes or max_bytes_from_config(None)

    if args.cmd == 'list':
        for d in list_drives(max_bytes, include_unsafe=True):
            print(f'{d.display():50} {"OK" if d.safe else "EXCLU : " + d.reason}')
        return 0

    lines = []

    def log(msg):
        lines.append(msg)
        print(msg)

    result = {'ok': False, 'log': lines}
    try:
        result['ok'] = format_drive(args.drive, args.label, args.expect_size, max_bytes, log)
    except SDFormatError as e:
        result['error'] = str(e)
    except Exception as e:  # remonté tel quel à l'interface
        result['error'] = f'{type(e).__name__} : {e}'
    if args.result:
        with open(args.result, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False)
    else:
        print(json.dumps({k: v for k, v in result.items() if k != 'log'}, ensure_ascii=False))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    sys.exit(_cli())
