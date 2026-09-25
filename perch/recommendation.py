"""Small, explainable local tag model; no feedback is uploaded to Wallhaven."""
from dataclasses import replace
import http.client
from itertools import islice
import logging
import math
from .tag_policy import is_spec_tag, tag_key
from . import colors

LOG = logging.getLogger('perch')
POOL_SIZE = 12
FEEDBACK_SYNC_LIMIT = 24
CALIBRATION_SAMPLES = 20
MIN_TAG_SAMPLES = 3


def learning_model(library, include_favorites=True, spec_policy=None, include_specs=False):
    """Withhold automatic inferences until enough independent feedback is available."""
    policy = spec_policy if spec_policy is not None else library.spec_policy()
    samples = library.learning_sample_count(include_favorites, policy)
    ready = samples >= CALIBRATION_SAMPLES
    raw = library.tag_profile(include_favorites, include_specs, policy)
    profile = [tag for tag in raw if tag['mode'] != 'auto' or tag['technical']
               or (ready and tag['positive'] + tag['negative'] >= MIN_TAG_SAMPLES)]
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
                    colors.save_palette(self.library, wid, item.get('colors', []))
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
    pending = [wid for wid in library.feedback() if fetcher.due(wid)]
    synced = 0
    for wid in pending[:FEEDBACK_SYNC_LIMIT]:
        if fetcher.get(wid) is not None:
            synced += 1
        if fetcher.unavailable:
            break
    if synced:
        LOG.info('已补全 %s 张反馈壁纸的标签', synced)
    return synced


def score_tags(tags, profile, spec_policy=is_spec_tag):
    weights = {tag_key(tag['name']): tag['weight'] for tag in profile if not spec_policy(tag['name'])}
    ignored = {tag_key(tag['name']) for tag in profile if tag.get('ignored') or spec_policy(tag['name'])}
    unique = {tag_key(tag['name']) for tag in tags or []
              if not spec_policy(tag['name']) and tag_key(tag['name']) not in ignored}
    # Neutral tags must not indirectly penalize a wallpaper through the divisor.
    return sum(weights.get(key, 0) for key in unique) / math.sqrt(max(1, len(unique)))


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


def order_pool(pool, profile, offset=0, spec_policy=is_spec_tag, color_profile=()):
    """Every fourth result retains source order to leave room for exploration."""
    scored = [(item, score_tags(tags, profile, spec_policy) + .65 * colors.score(item.get('colors', []), color_profile),
               index) for index, (item, tags) in enumerate(pool)]
    result = []
    while scored:
        if (offset + len(result) + 1) % 4 == 0:
            chosen = min(scored, key=lambda row: row[2])
        else:
            chosen = max(scored, key=lambda row: (row[1], -row[2]))
        scored.remove(chosen)
        result.append(chosen[0])
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
    streams = [client.candidates(config)]
    # Wallhaven exact-tag searches cannot be combined with a user's query.
    # Keep explicit queries intact and only rerank their results.
    if config.personalized and profile and not config.query.strip():
        for tag in [tag for tag in profile if tag['weight'] > 0 and not tag.get('ignored')
                    and not policy(tag['name'])][:2]:
            query = f"id:{tag['id']}" if tag['id'] is not None else tag['name']
            streams.append(optional_source(client.candidates(replace(config, query=query, max_pages=1))))
    preferred_colors = sorted((row for row in color_profile if row['weight'] > 0), key=lambda row: -row['weight'])
    if preferred_colors and not config.query.strip() and not config.color:
        streams.append(optional_source(client.candidates(replace(config, color=preferred_colors[0]['swatch'], max_pages=1))))
    encountered = set()

    def unseen():
        for item in interleave(streams):
            if not isinstance(item, dict) or not eligible(item, config):
                continue
            wid = item['id']
            if wid in encountered:
                continue
            encountered.add(wid)
            if not library.seen(wid=wid):
                yield item

    source = unseen()
    if not config.personalized or not any(tag['weight'] for tag in profile + color_profile):
        yield from source
        return
    offset = 0
    while pool := list(islice(source, POOL_SIZE)):
        enriched = []
        for item in pool:
            tags = fetcher.get(item['id']) if any(tag['weight'] for tag in profile) else library.tags_for(item['id'])
            if isinstance(item.get('colors'), list):
                colors.save_palette(library, item['id'], item['colors'])
            elif color_profile:
                cached = colors.palette(library, item['id'])
                if cached is not None:
                    item = dict(item, colors=cached)
            enriched.append((item, tags))
        LOG.info('个性化推荐 · 根据 %s 个标签排列 %s 张候选，保留探索机会',
                 sum(tag['weight'] != 0 for tag in profile), len(pool))
        yield from order_pool(enriched, profile, offset, policy, color_profile)
        offset += len(pool)
