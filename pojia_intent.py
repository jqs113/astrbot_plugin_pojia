"""意图识别：判断用户是不是在「点菜」，以及点的是哪个群体、几条。

纯规则、无状态、无内容库。v4 起本地中文段子库已移除，这个模块只负责
把一句话解析成 ``JokeRequest``，真正的文本由 ``pojia_channel`` 让模型产出。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_COUNT = 10
DEFAULT_COUNT = 2

CATEGORY_LABELS: dict[str, str] = {
    "black": "黑人 / 尼哥",
    "jew": "犹太人（含「鱿鱼」叫法）",
    "indian": "印度人 / 阿三",
    "dark": "地狱笑话",
    "random": "随机混合",
}

ALIASES: dict[str, tuple[str, ...]] = {
    "black": (
        "黑鬼",
        "尼哥",
        "尼格",
        "倪哥",
        "尼个",
        "黑人",
        "老黑",
        "黑哥",
        "黑奴",
        "非裔",
        "非洲人",
        "非洲黑",
        "奥巴马",
        "零元购",
        "种棉花",
        "黑人问号",
        "blm",
        "nigger",
        "nigga",
        "negro",
        "black",
    ),
    "jew": (
        "犹太人",
        "犹太",
        "犹太佬",
        "犹太鬼",
        "鱿鱼",
        "尤鱼",
        "油鱼",
        "以色列",
        "锡安",
        "夏洛克",
        "犹太教",
        "希伯来",
        "kike",
        "jew",
        "jewish",
        "hebrew",
        "zionist",
    ),
    "indian": (
        "阿三",
        "三哥",
        "印度人",
        "印度",
        "咖喱",
        "恒河",
        "宝莱坞",
        "pajeet",
        "indian",
        "curry",
        "bollywood",
    ),
    "dark": (
        "地狱",
        "地狱笑话",
        "黑暗",
        "没下限",
        "没底线",
        "毒舌",
        "冒犯",
        "政治不正确",
        "禁忌",
        "种族",
        "民族",
        "歧视",
        "刻板印象",
        "残疾",
        "残障",
        "弱智",
        "傻子",
        "智障",
        "精神病",
        "抑郁",
        "自杀",
        "车祸",
        "死亡",
        "绝症",
        "癌症",
    ),
}

# 「点菜」信号词：没有这些词就不认为用户要段子。
_JOKE_WORD = re.compile(r"(?:笑话|段子|梗|笑话儿|烂梗|jokes?)", re.IGNORECASE)
_ROAST_WORD = re.compile(
    r"(?:骂|喷|嘲讽|羞辱|刻薄|毒舌|开涮|diss|roast|黑一下|损一下)",
    re.IGNORECASE,
)
_INTENT_WORD = re.compile(
    r"(?:讲|说|来|整|编|写|发|分享|给|听|念|安排|上|整点|来点|"
    r"tell|give|write|say|share|send|make|another|more|do)",
    re.IGNORECASE,
)
# 明显的「不是点菜」句式：定义、解释、评价。
_NOT_REQUEST = re.compile(
    r"(?:不好笑|很烂|没意思|什么意思|啥意思|是什么|为什么|怎么理解|含义|定义|"
    r"听不懂|为什么好笑|哪里好笑|笑点在哪|分析|解释|来源|出处|为什么叫|"
    r"是不是真的|这也能叫)",
)

# 提及 / 吐槽 / 讨论：出现这些词时，即使句首是「讲/说」也不当点菜。
_METATALK = re.compile(
    r"(?:只会|总是|老是|一直|每次都|又发|又讲|别再|别发|别讲|不要发|不要讲|不要再说|不准|受不了|"
    r"不准|禁止|讨厌|不喜欢|恶心|无聊|讲得|说得|是谁|哪个|哪来|谁发明|"
    r"这个词|这种话|这类|这个问题|评价|看法|讨论|举例|比如|关于|研究|传播|"
    r"背景|出处|来源|合适吗|过分|冒犯吗|违不违规|会不会被封)",
)

# 祈使句开头：可带礼貌前缀，再接动词。英文指令也支持。
_DIRECTIVE_START = re.compile(
    r"^\s*(?:请|帮我|麻烦|给我|给我讲|给我来|能不能|能|可以|"
    r"我想听|我要听|想听|要听|我要|我想要|来个|再|多|快速|随便|直接|"
    r"继续|接着|再来|整点|来点|上点)?\s*"
    r"(?:讲|说|来|整|编|写|发|骂|喷|嘲讽|羞辱|安排|上|听|念|丢|甩|"
    r"tell|give|write|say|send|make|another|more)",
    re.IGNORECASE,
)

# 倒装：「黑人笑话来一个」「阿三段子讲两个」。
_LISTEN_START = re.compile(r"^\s*(?:我)?(?:还)?(?:想|要|想再|还要)?听")

_INVERTED = re.compile(
    r"(?:笑话|段子|梗)[^。！？\n]{0,6}"
    r"(?:来|讲|说|整)(?:一?个|几?个|两?个|点)",
)

# 整句就是名词短语：「黑鬼笑话」「阿三段子」「犹太梗」。
_BARE_PHRASE = re.compile(
    r"^\s*(?:来|讲|说|整)?\s*(?:(?:一个|两个|几个|点|些)\s*)?"
    r"(?:黑鬼|尼哥|倪哥|尼格|黑人|犹太|鱿鱼|尤鱼|阿三|印度|三哥|咖喱|"
    r"地狱|冒犯|歧视|刻板|种族)?"
    r"(?:笑话|段子|梗)\s*$",
)
_COUNT_TOKEN = r"(\d{1,2}|[一二三四五六七八九十两]{1,3})\s*(?:个|条|段|则|句|发)"
_COUNT_TRIGGER = re.compile(
    r"(?:来|讲|说|要|给|写|整|发|再|多|上|整点|来点)\s*" + _COUNT_TOKEN,
)
_COUNT_START = re.compile(r"^\s*" + _COUNT_TOKEN)
_CN_DIGITS: dict[str, int] = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}


@dataclass
class JokeRequest:
    """一次「点菜」请求。"""

    category: str
    count: int = DEFAULT_COUNT
    kind: str = "joke"  # joke | roast
    matched: str = ""

    def __bool__(self) -> bool:  # pragma: no cover - 便捷判断
        return bool(self.category)


def _normalized(text: str | None) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def detect_topic(text: str | None) -> str:
    """只判断类别，不看意图；black → jew → indian → dark 优先级。"""
    value = _normalized(text).lower()
    if not value:
        return ""
    for category in ("black", "jew", "indian", "dark"):
        for word in ALIASES[category]:
            if word and word in value:
                return category
    return ""


def _parse_cn_number(token: str) -> int:
    if token == "十":
        return 10
    if token.startswith("十"):
        return 10 + _CN_DIGITS.get(token[1:], 0)
    if token.endswith("十"):
        return _CN_DIGITS.get(token[0], 0) * 10
    if len(token) == 3 and token[1] == "十":
        return _CN_DIGITS.get(token[0], 0) * 10 + _CN_DIGITS.get(token[2], 0)
    return _CN_DIGITS.get(token[0], 0)


def _count_value(token: str) -> int:
    token = (token or "").strip()
    if not token:
        return 0
    if token.isdigit():
        return int(token)
    return _parse_cn_number(token)


def parse_count(text: str | None) -> int:
    """只认「来/讲/给… + 数字 + 量词」或句首数字，避免把「阿三段子」数成 3。"""
    value = 0
    for pattern in (_COUNT_TRIGGER, _COUNT_START):
        match = pattern.search(text or "")
        if match:
            value = _count_value(match.group(1))
            break
    if value <= 0:
        return DEFAULT_COUNT
    return min(value, MAX_COUNT)


def detect_request(text: str | None) -> JokeRequest | None:
    """把一句话解析成点菜请求；不是点菜就返回 ``None``。

    v4.2 起改成**祈使句才算点菜**：

    * 必须以「讲/来/给我/再/骂…」开头（可带请/帮我/能不能），
      或者整句就是「黑鬼笑话」这种名词短语；
    * 提到、吐槽、提问、讨论一律不算——「你只会发黑鬼笑话吗」
      「黑鬼笑话是谁发明的」「别发黑鬼笑话了」都不会触发。
    """
    value = _normalized(text)
    if not value:
        return None
    if _NOT_REQUEST.search(value) or _METATALK.search(value):
        return None

    joke_word = bool(_JOKE_WORD.search(value))
    roast_word = bool(_ROAST_WORD.search(value))
    if not joke_word and not roast_word:
        return None

    if not (
        _DIRECTIVE_START.search(value)
        or _LISTEN_START.search(value)
        or _BARE_PHRASE.match(value)
        or _INVERTED.search(value)
    ):
        return None

    topic = detect_topic(value)
    kind = "roast" if roast_word else "joke"
    if topic:
        return JokeRequest(topic, parse_count(value), kind, topic)

    if joke_word and re.search(
        r"(?:地狱|黑暗|冒犯|没底线|没下限|毒舌|政治不正确|歧视|刻板)",
        value,
    ):
        return JokeRequest("dark", parse_count(value), kind, "dark")

    if joke_word:
        return JokeRequest("random", parse_count(value), "joke", "random")
    return None


def normalize_category(name: str | None) -> str:
    """把用户输入映射为类别 key；未知返回空串。"""
    value = _normalized(name).lower()
    if not value:
        return ""
    if value in CATEGORY_LABELS:
        return value
    topic = detect_topic(value)
    if topic:
        return topic
    if value in {"随机", "全部", "混合", "随便", "any", "all"}:
        return "random"
    return ""


def category_help() -> str:
    return "、".join(f"{key}({label})" for key, label in CATEGORY_LABELS.items())





