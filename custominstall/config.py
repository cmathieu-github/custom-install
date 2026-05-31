import json
from os.path import join, isfile, abspath, dirname

_APP_ROOT = dirname(dirname(abspath(__file__)))

# config.json : configuration partagée entre tous les utilisateurs (à côté de run_gui.bat)
CONFIG_PATH = join(_APP_ROOT, 'config.json')

# defaults.json : valeurs initiales si config.json n'existe pas encore
DEFAULTS_PATH = join(_APP_ROOT, 'defaults.json')

# profiles.json : profils de configuration nommés (console enfant FR, adulte FR+EN, etc.)
PROFILES_PATH = join(_APP_ROOT, 'profiles.json')

_NDS_LANGUAGES = {
    'FR': 'Jeux NDS',
    'EN': 'Games NDS',
    'DE': 'Spiele NDS',
    'IT': 'Giochi NDS',
    'SP': 'Juegos NDS',
    'NL': 'Spellen NDS',
}

DEFAULTS = {
    'source_root': '',
    'nds_source_root': '',          # Si rempli, remplace source_root pour les packs NDS
    'nds_pack_order': ['32 Go', '64 Go+'],  # Ordre pour la logique additive NDS
    'nds_packs': {
        '32 Go': {
            'folder': '00_Pack_NDS_32Go',
            'base_folder': 'Base NDS',
            'languages': dict(_NDS_LANGUAGES),
        },
        '64 Go+': {
            'folder': '00_Pack_NDS_64Go',
            'base_folder': 'Base NDS',
            'languages': dict(_NDS_LANGUAGES),
        },
    },
    'cia_to_nds': {
        '32 Go':  '32 Go',
        '64 Go':  '64 Go+',
        '128 Go': '64 Go+',
        '256 Go': '64 Go+',
    },
    'cia_pack_order': ['32 Go', '64 Go', '128 Go', '256 Go'],
    'cia_packs': {
        '32 Go': {
            'folder': '00_Pack_3DS_32Go',
            'Base': '00_Pack_3DS_32Go_Base',
            'FR':   '00.1_Pack_3DS_32Go_FR',
            'EN':   '00.2_Pack_3DS_32Go_EN',
            'IT':   '00.3_Pack_3DS_32Go_IT',
            'SP':   '00.4_Pack_3DS_32Go_SP',
            'DE':   '00.5_Pack_3DS_32Go_DE',
            'NL':   '00.6_Pack_3DS_32Go_NL',
        },
        '64 Go': {
            'folder': '01_Pack_3DS_64Go',
            'Base': '01_Pack_3DS_64Go_Base',
            'FR':   '01.1_Pack_3DS_64Go_FR',
            'EN':   '01.2_Pack_3DS_64Go_EN',
            'IT':   '01.3_Pack_3DS_64Go_IT',
            'SP':   '01.4_Pack_3DS_64Go_SP',
            'DE':   '01.5_Pack_3DS_64Go_DE',
            'NL':   '01.6_Pack_3DS_64Go_NL',
        },
        '128 Go': {
            'folder': '02_Pack_3DS_128Go',
            'Base': '02_Pack_3DS_128Go_Base',
            'FR':   '02.1_Pack_3DS_128Go_FR',
            'EN':   '02.2_Pack_3DS_128Go_EN',
            'IT':   '02.3_Pack_3DS_128Go_IT',
            'SP':   '02.4_Pack_3DS_128Go_SP',
            'DE':   '02.5_Pack_3DS_128Go_DE',
            'NL':   '02.6_Pack_3DS_128Go_NL',
        },
        '256 Go': {
            'folder': '03_Pack_3DS_256Go',
            'Base': '03_Pack_3DS_256Go_Base',
            'FR':   '03.1_Pack_3DS_256Go_FR',
            'EN':   '03.2_Pack_3DS_256Go_EN',
            'IT':   '03.3_Pack_3DS_256Go_IT',
            'SP':   '03.4_Pack_3DS_256Go_SP',
            'DE':   '03.5_Pack_3DS_256Go_DE',
            'NL':   '03.6_Pack_3DS_256Go_NL',
        },
    },
    'custom_packs': ['', '', '', '', ''],
}


def _deep_copy_defaults():
    return {
        **DEFAULTS,
        'nds_packs': {
            k: {**v, 'languages': dict(v['languages'])}
            for k, v in DEFAULTS['nds_packs'].items()
        },
        'nds_pack_order': list(DEFAULTS['nds_pack_order']),
        'cia_to_nds': dict(DEFAULTS['cia_to_nds']),
        'cia_packs': {s: dict(v) for s, v in DEFAULTS['cia_packs'].items()},
        'cia_pack_order': list(DEFAULTS['cia_pack_order']),
        'custom_packs': list(DEFAULTS['custom_packs']),
    }


def _load_json(path):
    with open(path, 'r', encoding='utf-8') as f:
        # Ignore les clés commençant par "_" (commentaires dans le JSON)
        return {k: v for k, v in json.load(f).items() if not k.startswith('_')}


def load_config():
    config = _deep_copy_defaults()

    # Couche 1 : defaults.json (valeurs initiales, lues si config.json absent)
    if not isfile(CONFIG_PATH) and isfile(DEFAULTS_PATH):
        try:
            config.update(_load_json(DEFAULTS_PATH))
        except Exception:
            pass

    # Couche 2 : config.json (configuration partagée, écrite par l'interface)
    if isfile(CONFIG_PATH):
        try:
            config.update(_load_json(CONFIG_PATH))
        except Exception:
            pass

    return config


def save_config(config):
    data = {k: v for k, v in config.items() if not k.startswith('_')}
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_profiles():
    """Charge les profils sauvegardés depuis profiles.json.
    Retourne un dict {nom: config_dict}."""
    if isfile(PROFILES_PATH):
        try:
            with open(PROFILES_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_profiles(profiles):
    """Enregistre les profils dans profiles.json."""
    with open(PROFILES_PATH, 'w', encoding='utf-8') as f:
        json.dump(profiles, f, indent=2, ensure_ascii=False)
