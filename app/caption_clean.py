import re


_PUNCTUATION = re.compile(r"[\s，。！？、,.!?…~～—\-・:：;；\"'“”‘’（）()\[\]【】]+")
_FILLER_TOKENS = {
    "嗯", "嗯嗯", "嗯嗯嗯", "啊", "啊啊", "哦", "噢", "哎", "诶", "欸",
    "呃", "额", "唉", "哈", "哈哈", "哈哈哈", "哈哈哈哈", "嘿", "嘿嘿",
    "呵", "呵呵", "哼", "啧", "え", "ええ", "えー", "あ", "ああ", "うん",
    "うんうん", "うんうんうん", "へえ", "へー", "えっと", "あの", "その",
    "まあ", "ハ", "ハハ", "ハハハ", "ハハハハ",
}


def normalize_caption_token(value):
    return _PUNCTUATION.sub("", str(value or "")).lower()


def is_standalone_filler(zh="", ja=""):
    """Remove only explicit hesitation/reaction tokens, not meaningful short replies."""
    tokens=[normalize_caption_token(value) for value in (zh,ja)]
    tokens=[value for value in tokens if value]
    if not tokens:
        return False
    return all(
        value in _FILLER_TOKENS
        or re.fullmatch(r"(哈){1,4}",value)
        or re.fullmatch(r"(ハ){1,4}",value)
        for value in tokens
    )


def format_timecode(seconds):
    total_ms=max(0,int(round(float(seconds)*1000)))
    hours,remainder=divmod(total_ms,3_600_000)
    minutes,remainder=divmod(remainder,60_000)
    whole_seconds,milliseconds=divmod(remainder,1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}.{milliseconds:03d}"
