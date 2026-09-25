"""Tag identity and neutral display specifications shared by learning and ranking."""
import re
import unicodedata

# The settings list starts with common spellings; newly encountered resolution
# and aspect-ratio labels are also recognized by the rules below.
DEFAULT_SPEC_TAGS = (
    "2K", "4K", "5K", "8K", "16K", "HD", "Full HD", "UHD", "Ultra HD", "QHD", "WQHD",
    "high resolution", "720p", "1080p", "1440p", "2160p", "4320p",
    "1920x1080", "2560x1440", "3840x2160", "7680x4320", "16:9", "16:10", "21:9", "32:9",
    "ultrawide", "widescreen", "dual monitors", "JPEG", "PNG", "WebP", "高清", "超高清",
)


class SpecPolicy:
    """A snapshot shared by learning, discovery and candidate normalization."""
    def __init__(self, overrides=()):
        self.overrides = dict(overrides)

    def __call__(self, name):
        key = tag_key(name)
        return self.overrides[key] if key in self.overrides else is_spec_tag(name)


def validate_tag_name(name):
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 200 or any(ord(c) < 32 for c in name):
        raise ValueError("请输入有效的 Wallhaven 标签名称（最多 200 字符）")
    if re.search(r"(?:^|\s)(?:(?:id|like|type):|[@+\-])", tag_key(name)):
        raise ValueError("请填写标签名称，不要填写搜索表达式")
    return tag_key(name)


def tag_key(name):
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


def is_spec_tag(name):
    """Match whole specification labels, never arbitrary words containing '4k'."""
    key = tag_key(name)
    key = re.sub(r"\s*[-_]\s*", " ", key)
    # Remove only edge wrappers. Words like 'wide' or 'dual' alone are content,
    # while 'wide screen' and 'dual monitors' describe display specifications.
    key = re.sub(r"^wallpapers?\s+|\s+wallpapers?$", "", key)
    if key in {"hd", "fhd", "uhd", "qhd", "wqhd", "full hd", "ultra hd", "quad hd",
               "fullhd", "ultrahd", "high resolution", "high res", "high definition",
               "ultra high definition", "高清", "超清", "超高清", "高分辨率",
               "ultrawide", "ultra wide", "widescreen", "wide screen",
               "jpg", "jpeg", "png", "webp"}:
        return True
    if re.fullmatch(r"(?:dual|triple|multi|ultrawide|ultra wide|wide)\s+(?:monitors?|screens?|displays?)", key):
        return True
    key = re.sub(r"^(?:resolution|pixels?|display|screen|monitor)\s+", "", key)
    key = re.sub(r"\s+(?:resolution|pixels?|display|screen|monitor)$", "", key)
    if re.fullmatch(r"(?:(?:ultra\s*hd|uhd|hd)\s*)?(?:2|4|5|6|8|10|12|16)\s*k"
                    r"(?:\s*(?:ultra\s*hd|uhd|hd))?", key):
        return True
    if re.fullmatch(r"(?:480|720|1080|1440|2160|4320)\s*[pi]", key):
        return True
    if re.fullmatch(r"\d{3,5}\s*[x×*]\s*\d{3,5}(?:\s*px)?", key):
        return True
    return bool(re.fullmatch(r"\d{1,2}\s*[:x]\s*\d{1,2}", key))
