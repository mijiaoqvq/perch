"""Small, explainable local tag model; no feedback is uploaded to Wallhaven."""
from dataclasses import replace
import http.client
from itertools import islice
import logging
import math

LOG = logging.getLogger('perch')
POOL_SIZE = 12
FEEDBACK_SYNC_LIMIT = 24


class TagFetcher:
    def __init__(self, library, client):
        self.library = library
        self.client = client
        self.unavailable = False

    def get(self, wid):
        cached = self.library.tags_for(wid)
        if cached is not None:
            return cached
        if self.unavailable or not self.library.tags_due(wid):
            return None
        # One metadata request at a time across the GUI and scheduled worker.
        with self.library.locked('metadata.lock'):
            cached = self.library.tags_for(wid)
            if cached is not None:
                return cached
            if not self.library.tags_due(wid):
                return None
            try:
                tags = self.client.tags(wid)
                self.library.save_tags(wid, tags)
            except (OSError, ValueError, http.client.HTTPException) as exc:
                self.unavailable = True
                self.library.tag_failure(wid)
                LOG.warning('标签暂不可用，保留反馈并使用已有标签：%s · %s', wid, exc)
                return None
        return self.library.tags_for(wid)


def sync_feedback(config, library, fetcher):
    """Backfill feedback on old or already deleted images, with a per-run budget."""
    pending = [wid for wid in library.feedback(config.favorites_influence) if library.tags_due(wid)]
    synced = 0
    for wid in pending[:FEEDBACK_SYNC_LIMIT]:
        if fetcher.get(wid) is not None:
            synced += 1
        if fetcher.unavailable:
            break
    if synced:
        LOG.info('已补全 %s 张反馈壁纸的标签', synced)
    return synced


def score_tags(tags, profile):
    weights = {tag['id']: tag['weight'] for tag in profile}
    unique = {tag['id'] for tag in tags or []}
    return sum(weights.get(tid, 0) for tid in unique) / math.sqrt(max(1, len(unique)))


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


def order_pool(pool, profile, offset=0):
    """Every fourth result retains source order to leave room for exploration."""
    scored = [(item, score_tags(tags, profile), index) for index, (item, tags) in enumerate(pool)]
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
    if config.personalized:
        sync_feedback(config, library, fetcher)
        profile = library.tag_profile(config.favorites_influence)
    streams = [client.candidates(config)]
    # Wallhaven exact-tag searches cannot be combined with a user's query.
    # Keep explicit queries intact and only rerank their results.
    if config.personalized and profile and not config.query.strip():
        for tag in [tag for tag in profile if tag['weight'] > 0][:2]:
            streams.append(optional_source(client.candidates(replace(config, query=f"id:{tag['id']}", max_pages=1))))
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
    if not config.personalized or not profile:
        yield from source
        return
    offset = 0
    while pool := list(islice(source, POOL_SIZE)):
        enriched = []
        for item in pool:
            tags = fetcher.get(item['id'])
            enriched.append((item, tags))
        LOG.info('个性化推荐 · 根据 %s 个标签排列 %s 张候选，保留探索机会', len(profile), len(pool))
        yield from order_pool(enriched, profile, offset)
        offset += len(pool)
