"""Conservative, shared evidence model for content tags and colour families.

Weights are ranking heuristics, not calibrated probabilities of liking an image.
Explicit judgements determine direction; passive acceptance can only break ties.
"""
import math
import time

CALIBRATION_SAMPLES = 20
MIN_TAG_SAMPLES = 3
EXPOSURE_SECONDS = 10
EXPLICIT_HALF_LIFE = 90 * 86400
IMPLICIT_HALF_LIFE = 30 * 86400
IMPLICIT_UNIT = .05
IMPLICIT_MASS_CAP = .5
IMPLICIT_MAX_BOOST = .04
MANUAL_WEIGHTS = {'prefer': 1., 'avoid': -1.5, 'ignore': 0.}


def decay(created, now, half_life):
    return 2 ** (-max(0., now - created) / half_life)


def direction(positive, negative, other_positive=0., other_negative=0.):
    """Symmetric Wilson-style uncertainty band, with a practical neutral zone.

    Fractional, time-decayed evidence is deliberately treated as fewer samples.
    The interval is an uncertainty guard, not a statistical coverage guarantee
    for biased/correlated recommendation feedback.
    """
    total = positive + negative
    if total < 2:
        return 0., '有效证据不足，暂时中立'
    probability = positive / total
    z = 1.645
    denominator = 1 + z * z / total
    center = (probability + z * z / (2 * total)) / denominator
    radius = z * math.sqrt(probability * (1 - probability) / total + z * z / (4 * total * total)) / denominator
    low, high = center - radius, center + radius
    if low > .55:
        weight = (low - .55) / .45
    elif high < .45:
        weight = -(.45 - high) / .45
    else:
        return 0., '正负反馈接近或证据不足，暂时中立'
    # A tag shared by nearly everything the user rejects is not automatically
    # the reason for rejection. Compare against rated images without this tag.
    others = other_positive + other_negative
    if others >= 5:
        baseline = (other_positive + 2) / (others + 4)
        estimate = (positive + 2) / (total + 4)
        lift = (estimate - baseline) * (1 if weight > 0 else -1)
        if lift <= .05:
            return 0., '与其他壁纸相比没有明确偏好差异，暂时中立'
        weight *= min(1., (lift - .05) / .25)
    weight *= total / (total + 4)
    return weight, '明确反馈支持更多推荐' if weight > 0 else '明确反馈支持减少推荐'


def feature_model(library, features, overrides, include_favorites=True, now=None):
    """One contribution per image/feature; explicit and passive counts stay separate."""
    now = time.time() if now is None else now
    feedback = library.feedback_records(include_favorites)
    accepted = library.accepted_feedback()
    usable = {wid: {key: amount for key, amount in values.items() if overrides.get(key) != 'ignore'}
              for wid, values in features.items()}
    rated = {wid: entry for wid, entry in feedback.items() if usable.get(wid)}
    samples = len(rated)
    total_positive = total_negative = 0.
    counts = {}

    def entry(key):
        return counts.setdefault(key, dict(positive=0, negative=0, accepted=0,
                                 positive_mass=0., negative_mass=0., accepted_mass=0.,
                                 present_positive=0., present_negative=0.))

    for wid, (sign, created) in rated.items():
        mass = decay(created, now, EXPLICIT_HALF_LIFE)
        if sign > 0:
            total_positive += mass
        else:
            total_negative += mass
        for key, amount in usable[wid].items():
            row = entry(key)
            polarity = 'positive' if sign > 0 else 'negative'
            row[polarity] += 1
            row[polarity + '_mass'] += mass * amount
            row['present_' + polarity] += mass
    for wid, created in accepted.items():
        if wid in feedback:
            continue
        for key, amount in usable.get(wid, {}).items():
            row = entry(key)
            row['accepted'] += 1
            row['accepted_mass'] += amount * decay(created, now, IMPLICIT_HALF_LIFE)
    # Keep feedback counts visible even after the user removes a preference.
    for wid, (sign, _) in feedback.items():
        for key in features.get(wid, {}):
            if overrides.get(key) == 'ignore':
                entry(key)['positive' if sign > 0 else 'negative'] += 1
    for wid in accepted.keys() - feedback.keys():
        for key in features.get(wid, {}):
            if overrides.get(key) == 'ignore':
                entry(key)['accepted'] += 1
    for key in overrides:
        entry(key)
    for key, row in counts.items():
        mode = overrides.get(key, 'auto')
        calibrated = samples >= CALIBRATION_SAMPLES and row['positive'] + row['negative'] >= MIN_TAG_SAMPLES
        weight, reason = direction(row['positive_mass'], row['negative_mass'],
                                   total_positive - row['present_positive'],
                                   total_negative - row['present_negative'])
        if not calibrated:
            weight = 0.
            reason = '自动校准中，暂不推断偏好' if samples < CALIBRATION_SAMPLES else '明确反馈不足，暂时中立'
        implicit = 0.
        if (samples >= CALIBRATION_SAMPLES and row['accepted'] >= 3
                and (row['negative'] == 0 or weight > 0)):
            implicit = IMPLICIT_MAX_BOOST * min(IMPLICIT_MASS_CAP, IMPLICIT_UNIT * row['accepted_mass']) / IMPLICIT_MASS_CAP
        row.update(mode=mode, calibrated=calibrated, auto_weight=weight,
                   weight=MANUAL_WEIGHTS.get(mode, weight), reason=reason,
                   implicit_weight=implicit if mode == 'auto' else 0.)
    return samples, counts
