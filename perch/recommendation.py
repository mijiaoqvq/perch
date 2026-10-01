"""Small, explainable local tag model; no feedback is uploaded to Wallhaven."""
from dataclasses import replace
import http.client
from itertools import islice
import logging
import random
from .tag_policy import is_spec_tag, tag_key
from . import colors
from .learning import CALIBRATION_SAMPLES, MIN_TAG_SAMPLES, IMPLICIT_MAX_BOOST

LOG = logging.getLogger('perch')
POOL_SIZE = 12
FEEDBACK_SYNC_LIMIT = 24


def learning_model(library, include_favorites=True, spec_policy=None, include_specs=False):
    """Withhold automatic inferences until enough independent feedback is available."""
    policy = spec_policy if spec_policy is not None else library.spec_policy()
    samples = library.learning_sample_count(include_favorites, policy)
    ready = samples >= CALIBRATION_SAMPLES
    raw = library.tag_profile(include_favorites, include_specs, policy)
    profile = [tag for tag in raw if tag['mode'] != 'auto' or tag['technical']
               or (ready and max(tag['positive'] + tag['negative'], tag['accepted']) >= MIN_TAG_SAMPLES)]
    return samples, profile


class TagFetcher:
    def __init__(self, library, client):
        self.library = library
        self.client = client
        self.unavailable = False

    def get(self, wid):
        cached = self.library.tags_for(wid)
        details = callable(getattr(self.client, 'detail', None))
        needs_color = details and colors.palette(self.library, wid) is None
        if cached is not None and not needs_color:
            return cached
        if self.unavailable or not self.due(wid):
            return cached
        # One metadata request at a time across the GUI and scheduled worker.
        with self.library.locked('metadata.lock'):
            cached = self.library.tags_for(wid)
            if cached is not None and (not details or colors.palette(self.library, wid) is not None):
                return cached
            if not self.due(wid):
                return cached
            try:
                if details:
                    item = self.client.detail(wid)
                    tags = item['tags']
                    palette = item.get('colors')
                    colors.save_palette(self.library, wid, palette if isinstance(palette, list) else [])
                else:
                    tags = self.client.tags(wid)
                self.library.save_tags(wid, tags)
            except (OSError, ValueError, http.client.HTTPException) as exc:
                self.unavailable = True
                self.library.tag_failure(wid)
                LOG.warning('标签暂不可用，保留反馈并使用已有标签：%s · %s', wid, exc)
                return cached
        return self.library.tags_for(wid)

    def due(self, wid):
        if self.library.tags_due(wid):
            return True
        if callable(getattr(self.client, 'detail', None)) and colors.palette(self.library, wid) is None:
            import time
            with self.library.connect() as db:
                row = db.execute('SELECT retry_after FROM tag_cache WHERE id=?', (wid,)).fetchone()
            return not row or row[0] <= time.time()
        return False


def sync_feedback(config, library, fetcher):
    """Backfill feedback on old or already deleted images, with a per-run budget."""
    pending = [wid for wid in dict.fromkeys([*library.feedback(), *library.accepted_feedback()]) if fetcher.due(wid)]
    synced = 0
    for wid in pending[:FEEDBACK_SYNC_LIMIT]:
        if fetcher.get(wid) is not None:
            synced += 1
        if fetcher.unavailable:
            break
    if synced:
        LOG.info('已补全 %s 张反馈壁纸的标签', synced)
    return synced


def score_tags(tags, profile, spec_policy=is_spec_tag, implicit=False):
    field = 'implicit_weight' if implicit else 'weight'
    weights = {tag_key(tag['name']): tag.get(field, 0.) for tag in profile if not spec_policy(tag['name'])}
    ignored = {tag_key(tag['name']) for tag in profile if tag.get('ignored') or spec_policy(tag['name'])}
    unique = {tag_key(tag['name']) for tag in tags or []
              if not spec_policy(tag['name']) and tag_key(tag['name']) not in ignored}
    # Correlated synonyms/extra tags cannot accumulate unbounded rewards. Unknown
    # and neutral tags have no effect on either score or normalization.
    values = [weights.get(key, 0.) for key in unique]
    return max([0., *values]) + min([0., *values])


def interleave(streams):
    streams = [iter(stream) for stream in streams]
    while streams:
        for stream in streams[:]:
            try:
                yield next(stream)
            except StopIteration:
                streams.remove(stream)


def optional_source(stream):
    try:
        yield from stream
    except (OSError, ValueError, http.client.HTTPException) as exc:
        LOG.warning('偏好标签搜索暂不可用，继续普通发现：%s', exc)


def content_features(item, tags, spec_policy=is_spec_tag, ignored=()):
    result = {f'tag:{tag_key(tag["name"])}': 1. for tag in tags or []
              if not spec_policy(tag['name']) and tag_key(tag['name']) not in ignored}
    result.update({f'color:{key}': .25 * amount for key, amount in colors.families(item.get('colors', [])).items()
                   if f'color:{key}' not in ignored})
    return result


def similarity(left, right):
    keys = left.keys() | right.keys()
    return sum(min(left.get(key, 0), right.get(key, 0)) for key in keys) / max(1., sum(
        max(left.get(key, 0), right.get(key, 0)) for key in keys))


def choose(pool, profile, offset, spec_policy, color_profile, recent):
    exploration = (offset + 1) % 4 == 0
    if exploration:
        # Ordinary discovery, not the first item of a biased mixed stream.
        index = next((i for i, (item, _) in enumerate(pool)
                      if item.get('_perch_source', 'discovery') == 'discovery'), 0)
        return index, True
    ignored = {tag_key(row['name']) for row in profile if row.get('ignored')}
    ignored.update('color:' + row['key'] for row in color_profile if row['mode'] == 'ignore')
    def score(pair):
        item, tags = pair
        explicit = score_tags(tags, profile, spec_policy) + .65 * colors.score(item.get('colors', []), color_profile)
        weak = min(IMPLICIT_MAX_BOOST, score_tags(tags, profile, spec_policy, implicit=True)
                   + .65 * colors.score(item.get('colors', []), color_profile, implicit=True))
        if explicit < 0:
            weak = 0.  # Passive tolerance cannot rescue an explicitly negative match.
        features = content_features(item, tags, spec_policy, ignored)
        repetition = max((similarity(features, previous) for previous in recent), default=0.)
        return explicit + weak - .2 * repetition
    return max(range(len(pool)), key=lambda index: (score(pool[index]), -index)), False


def order_pool(pool, profile, offset=0, spec_policy=is_spec_tag, color_profile=()):
    """Offline batch ordering, also useful for inspecting the ranking policy."""
    remaining, recent = list(pool), []
    result = []
    while remaining:
        index, _ = choose(remaining, profile, offset + len(result), spec_policy, color_profile, recent)
        item, tags = remaining.pop(index)
        result.append(item)
        recent.append(content_features(item, tags, spec_policy))
    return result


def candidates(config, library, client, fetcher, eligible):
    """Mix tag searches with ordinary discovery, then rank a bounded lookahead."""
    profile = []
    color_profile = []
    policy = library.spec_policy()
    if config.personalized:
        sync_feedback(config, library, fetcher)
        samples, profile = learning_model(library, spec_policy=policy)
        _, color_profile = colors.learning_model(library)
        if samples < CALIBRATION_SAMPLES:
            LOG.info('自动校准中 · 已收集 %s / %s 张有效反馈；自动标签偏好暂不参与推荐',
                     samples, CALIBRATION_SAMPLES)
    streams = []
    # Wallhaven exact-tag searches cannot be combined with a user's query.
    # Keep explicit queries intact and only rerank their results.
    if config.personalized and profile and not config.query.strip():
        preferred = [tag for tag in profile if tag['weight'] >= .08 and not tag.get('ignored')
                     and not policy(tag['name'])][:8]
        for tag in random.sample(preferred, min(2, len(preferred))):
            query = f"id:{tag['id']}" if tag['id'] is not None else tag['name']
            streams.append(optional_source(client.candidates(replace(config, query=query, max_pages=1))))
    preferred_colors = sorted((row for row in color_profile if row['weight'] >= .08), key=lambda row: -row['weight'])[:4]
    if preferred_colors and not config.query.strip() and not config.color:
        streams.append(optional_source(client.candidates(replace(config, color=random.choice(preferred_colors)['swatch'], max_pages=1))))
    encountered = set()

    def unseen(stream, origin):
        for item in stream:
            if not isinstance(item, dict) or not eligible(item, config):
                continue
            wid = item['id']
            if wid in encountered:
                continue
            encountered.add(wid)
            if not library.seen(wid=wid):
                yield dict(item, _perch_source=origin)

    source = unseen(client.candidates(config), 'discovery')
    if not config.personalized or not any(tag['weight'] or tag.get('implicit_weight') for tag in profile + color_profile):
        yield from source
        return
    biased = unseen(interleave(streams), 'preference')
    def enrich(item):
        tags = fetcher.get(item['id']) if any(tag['weight'] or tag.get('implicit_weight') for tag in profile) else library.tags_for(item['id'])
        if isinstance(item.get('colors'), list):
            colors.save_palette(library, item['id'], item['colors'])
        else:
            cached = colors.palette(library, item['id'])
            item = dict(item, colors=cached or [])
        return item, tags
    while True:
        # At least half the pool comes from ordinary discovery when available.
        pool = list(islice(source, POOL_SIZE // 2))
        pool += list(islice(biased, POOL_SIZE - len(pool)))
        pool += list(islice(source, POOL_SIZE - len(pool)))
        if not pool:
            return
        enriched = [enrich(item) for item in pool]
        LOG.info('个性化推荐 · 根据 %s 个标签排列 %s 张候选，保留探索机会',
                 sum(tag['weight'] != 0 for tag in profile), len(pool))
        ignored = {tag_key(row['name']) for row in profile if row.get('ignored')}
        ignored.update('color:' + row['key'] for row in color_profile if row['mode'] == 'ignore')
        while enriched:
            count = library.delivery_count()
            if (count + 1) % 4 == 0 and not any(item['_perch_source'] == 'discovery' for item, _ in enriched):
                extra = next(source, None)
                if extra is not None:
                    enriched.append(enrich(extra))
            recent = [content_features({'colors': colors.palette(library, wid) or []}, library.tags_for(wid), policy, ignored)
                      for wid in library.recent_deliveries()]
            # Re-read after each yield: only a successfully saved image advances
            # the durable counter. Failures, duplicate hashes and restarts do not.
            index, exploration = choose(enriched, profile, count, policy, color_profile, recent)
            item, _ = enriched.pop(index)
            if exploration and item['_perch_source'] == 'discovery':
                item = dict(item, _perch_source='exploration')
            yield item
