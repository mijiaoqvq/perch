"""Explainable colour families from Wallhaven's ordered dominant palette."""
import colorsys
import json
import re

# Representative swatches are also values supported by Wallhaven's search API.
TONES = {
    'red': ('红色', 'cc3333'), 'orange': ('橙色', 'ff9900'),
    'yellow': ('黄色', 'ffcc33'), 'green': ('绿色', '77cc33'),
    'cyan': ('青色', '66cccc'), 'blue': ('蓝色', '0066cc'),
    'purple': ('紫色', '663399'), 'pink': ('粉色', 'ea4c88'),
    'brown': ('棕色', '996633'), 'black': ('深黑', '000000'),
    'gray': ('灰色', '999999'), 'white': ('浅白', 'ffffff'),
}
SEARCH_COLORS = frozenset('660000 990000 cc0000 cc3333 ea4c88 993399 663399 333399 '
                         '0066cc 0099cc 66cccc 77cc33 669900 336600 666600 999900 '
                         'cccc33 ffff00 ffcc33 ff9900 ff6600 cc6633 996633 663300 '
                         '000000 999999 cccccc ffffff 424153'.split())


def normalize(colors):
    if not isinstance(colors, list):
        raise ValueError('无法识别壁纸色调')
    return list(dict.fromkeys(c.lstrip('#').lower() for c in colors
                             if isinstance(c, str) and re.fullmatch(r'#?[a-fA-F0-9]{6}', c)))[:10]


def family(color):
    r, g, b = (int(color[i:i + 2], 16) / 255 for i in (0, 2, 4))
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    h *= 360
    if v < .16:
        return 'black'
    if s < .13:
        return 'white' if v > .82 else ('black' if v < .32 else 'gray')
    if 15 <= h < 50 and v < .68:
        return 'brown'
    if h < 15 or h >= 350:
        return 'red'
    if h < 45:
        return 'orange'
    if h < 70:
        return 'yellow'
    if h < 165:
        return 'green'
    if h < 195:
        return 'cyan'
    if h < 255:
        return 'blue'
    if h < 290:
        return 'purple'
    return 'pink'


def families(colors):
    # Only the leading three colours affect learning; a small accent has less sway.
    result = {}
    for index, color in enumerate(normalize(colors or [])[:3]):
        key = family(color)
        result.setdefault(key, (1.0, .55, .3)[index])
    return result


def save_palette(library, wid, colors):
    if not re.fullmatch(r'[a-z0-9]{6}', wid):
        raise ValueError('无效的壁纸 ID')
    with library.connect() as db:
        db.execute('INSERT INTO wallpaper_colors VALUES (?, ?) ON CONFLICT(id) '
                   'DO UPDATE SET colors=excluded.colors', (wid, json.dumps(normalize(colors))))


def palette(library, wid):
    with library.connect() as db:
        row = db.execute('SELECT colors FROM wallpaper_colors WHERE id=?', (wid,)).fetchone()
    return json.loads(row[0]) if row else None


def set_override(library, key, mode):
    if key not in TONES or mode not in (None, 'prefer', 'avoid', 'ignore'):
        raise ValueError('未知的色调偏好')
    with library.connect() as db:
        if mode is None:
            db.execute('DELETE FROM color_overrides WHERE name=?', (key,))
        else:
            db.execute('INSERT INTO color_overrides VALUES (?, ?) ON CONFLICT(name) '
                       'DO UPDATE SET mode=excluded.mode', (key, mode))


def learning_model(library):
    from .recommendation import CALIBRATION_SAMPLES, MIN_TAG_SAMPLES
    feedback = library.feedback()
    counts = {key: [0, 0, 0., 0.] for key in TONES}
    samples = set()
    with library.connect() as db:
        overrides = dict(db.execute('SELECT name, mode FROM color_overrides'))
        for wid, raw in db.execute('SELECT id, colors FROM wallpaper_colors'):
            if wid not in feedback:
                continue
            tones = {key: amount for key, amount in families(json.loads(raw)).items()
                     if overrides.get(key) != 'ignore'}
            if tones:
                samples.add(wid)
            for key, amount in tones.items():
                sign = 0 if feedback[wid] > 0 else 1
                counts[key][sign] += 1
                counts[key][sign + 2] += amount
    profile = []
    for key, (name, swatch) in TONES.items():
        pos, neg, pos_amount, neg_amount = counts[key]
        mode = overrides.get(key, 'auto')
        calibrated = len(samples) >= CALIBRATION_SAMPLES and pos + neg >= MIN_TAG_SAMPLES
        weight = (pos_amount - 1.5 * neg_amount) / (pos + neg + 2) if calibrated else 0.
        weight = {'prefer': 1., 'avoid': -1.5, 'ignore': 0.}.get(mode, weight)
        profile.append(dict(key=key, name=name, swatch=swatch, mode=mode,
                            positive=pos, negative=neg, weight=weight, calibrated=calibrated))
    return len(samples), profile


def score(colors, profile):
    weights = {row['key']: row['weight'] for row in profile if row['mode'] != 'ignore'}
    tones = {key: amount for key, amount in families(colors).items() if key in weights}
    return sum(weights[key] * amount for key, amount in tones.items()) / max(1., sum(tones.values()))
