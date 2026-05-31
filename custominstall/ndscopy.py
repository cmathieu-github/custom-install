import os
import shutil
import time
from os.path import join, isdir

from events import Events


class NDSCopier:
    def __init__(self, source_root, base_folder, language_folders):
        self.event = Events()
        self.source_root = source_root
        self.base_folder = base_folder
        self.language_folders = language_folders

    def _total_size(self, *paths):
        total = 0
        for path in paths:
            for dirpath, _, filenames in os.walk(path):
                for f in filenames:
                    try:
                        total += os.path.getsize(join(dirpath, f))
                    except OSError:
                        pass
        return total

    def _copy_tree(self, src, dst, cancelled, state):
        os.makedirs(dst, exist_ok=True)
        for dirpath, _, filenames in os.walk(src):
            if cancelled[0]:
                return
            rel = os.path.relpath(dirpath, src)
            target_dir = dst if rel == '.' else join(dst, rel)
            os.makedirs(target_dir, exist_ok=True)
            for filename in filenames:
                if cancelled[0]:
                    return
                src_file = join(dirpath, filename)
                dst_file = join(target_dir, filename)
                try:
                    file_size = os.path.getsize(src_file)
                except OSError:
                    file_size = 0
                shutil.copy2(src_file, dst_file)
                state['copied'] += file_size
                elapsed = time.time() - state['start']
                speed = state['copied'] / elapsed if elapsed > 0 else 0
                self.event.on_progress(state['copied'], state['total'], speed, filename)

    def start(self, language, target_path, cancelled):
        lang_folder = self.language_folders.get(language)
        if not lang_folder:
            self.event.on_error(f'Langue inconnue : {language}')
            return False

        base_src = join(self.source_root, self.base_folder)
        lang_src = join(self.source_root, lang_folder)
        dst = join(target_path, lang_folder)

        if not isdir(base_src):
            self.event.on_error(f'Dossier source introuvable :\n{base_src}')
            return False
        if not isdir(lang_src):
            self.event.on_error(f'Dossier langue introuvable :\n{lang_src}')
            return False

        self.event.on_log('Calcul de la taille totale...')
        total = self._total_size(base_src, lang_src)
        self.event.on_log(f'Taille totale : {total / (1024 ** 3):.2f} Go')
        self.event.on_log(f'Destination : {dst}')

        state = {'copied': 0, 'total': total, 'start': time.time()}

        self.event.on_log('Copie de Base NDS...')
        self._copy_tree(base_src, dst, cancelled, state)

        if not cancelled[0]:
            self.event.on_log(f'Copie de {lang_folder}...')
            self._copy_tree(lang_src, dst, cancelled, state)

        elapsed = time.time() - state['start']
        avg_speed = state['copied'] / elapsed if elapsed > 0 else 0
        self.event.on_done(elapsed, avg_speed, dst, cancelled[0])
        return True
