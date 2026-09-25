"""Tag identity and neutral display specifications shared by learning and ranking."""
import re
import unicodedata


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
