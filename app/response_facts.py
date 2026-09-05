"""User-owned facts and conservative corrections shared by both delivery paths."""
from __future__ import annotations

from dataclasses import dataclass
import re

COUNT = r'(?:\d+|[零〇一二兩三四五六七八九十百千萬]+)'
STAGES = {'私訊': 'messages', '訊息': 'messages', '預約': 'bookings', '到店': 'attendance',
          '來店': 'attendance', '曝光': 'impressions', '點擊': 'clicks'}
DELIVERABLE = re.compile(r'話術|文案|追蹤訊息|提醒|開場白|安撫|幫我(?:寫|回|縮短)|只要一句')
DAY = (r'(?:這|本|下)?(?:週|星期|禮拜)[一二三四五六日天](?:[、和至到]?[一二三四五六日天])*'
       r'|明天|後天|今天|\d{1,4}[/-]\d{1,2}(?:[/-]\d{1,2})?|\d{1,2}月\d{1,2}[日號]')
PERIOD = r'(?:上午|下午|晚上|早上|中午|傍晚)'
CLOCK = (r'(?:\d{1,2}[:：]\d{2}|(?:\d{1,2}|[零〇一二兩三四五六七八九十]{1,3})點'
         r'(?:半|一刻|三刻|(?:\d{1,2}|[零〇一二兩三四五六七八九十]{1,3})分)?)')
SPECIFIC = re.compile(
    rf'(?P<date_time>(?:{DAY})(?:\s*{PERIOD})?(?:\s*{CLOCK})?|(?:{PERIOD})?{CLOCK})'
    r'|(?P<price>(?:價格|報價|費用|收費|我只回|只要|總共)(?:已)?(?:改成|改為|調成|調為|是|為)?\s*\d[\d,]*(?:元|塊)?'
    r'|(?:NT\$|\$)\s*[\d,]+|\d[\d,]*(?:元|塊))'
    r'|(?P<store>(?:店名(?:是|叫|為)?|我們在|地址(?:是|為)?)\s*[A-Za-z\u4e00-\u9fff0-9]{2,24})'
)
RATE = re.compile(r'(?P<name>(?:私訊轉預約|私訊預約|預約轉到店|私訊到店|到店|預約|點擊)率)'
                  r'\s*(?:(?:大約|大概|約|為|是|[:：=])\s*)*(?P<value>\d+(?:\.\d+)?)\s*[%％]')
TIME_CONTEXT = re.compile(r'昨天|之前|以前|原本|今天|現在|目前|改成|改為')


def _currentness(text: str, position: int) -> int:
    anchors = list(TIME_CONTEXT.finditer(text, 0, position))
    return int(not anchors or anchors[-1][0] not in ('昨天', '之前', '以前', '原本'))


RATE_STAGES = {
    '私訊轉預約率': ('bookings', 'messages'), '私訊預約率': ('bookings', 'messages'),
    '預約率': ('bookings', 'messages'), '到店率': ('attendance', 'messages'),
    '私訊到店率': ('attendance', 'messages'), '預約轉到店率': ('attendance', 'bookings'),
    '點擊率': ('clicks', 'impressions'),
}


def chinese_count(text: str) -> int:
    if text.isdigit():
        return int(text)
    digits = dict(zip('零〇一二兩三四五六七八九', (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
    total = section = number = 0
    for char in text:
        if char in digits:
            number = digits[char]
        else:
            unit = {'十': 10, '百': 100, '千': 1000, '萬': 10000}[char]
            if unit == 10000:
                total += (section + number) * unit
                section = number = 0
            else:
                section += (number or 1) * unit
                number = 0
    return total + section + number


def user_texts(question: str, history=None) -> list[str]:
    return [str(item.get('content', '')) for item in history or [] if item.get('role') == 'user'] + [question]


def stage_counts(question: str, history=None) -> dict[str, int]:
    facts = {}
    labels = '|'.join(STAGES)
    forward = re.compile(rf'({labels})(?:人數|數)?(?:只有|是|有|共|了)?\s*({COUNT})(?![\d零〇一二兩三四五六七八九十百千萬年月天])')
    backward = re.compile(rf'({COUNT})\s*(?:個|位|人|則)\s*({labels})')
    zero = re.compile(r'(?:完全)?(?:沒有人|沒人|沒有|零|沒|無)(?:來)?(?:私訊|訊息)|(?:私訊|訊息)(?:完全)?(?:沒有|零|沒人)')
    absent = re.compile(r'(?:還沒|尚未|未)(?:到店|來店)')
    for turn, text in enumerate(user_texts(question, history)):
        matches = [(m.start(), STAGES[m[1]], chinese_count(m[2])) for m in forward.finditer(text)]
        matches += [(m.start(), STAGES[m[2]], chinese_count(m[1])) for m in backward.finditer(text)]
        matches += [(m.start(), 'messages', 0) for m in zero.finditer(text)]
        matches += [(m.start(), 'attendance', None) for m in absent.finditer(text)]
        for position, stage, count in matches:
            order = (_currentness(text, position), turn, position)
            if stage not in facts or order >= facts[stage][0]:
                facts[stage] = (order, count)
    return {stage: value for stage, (_, value) in facts.items() if value is not None}


@dataclass(frozen=True)
class Metric:
    name: str
    numerator: int
    denominator: int

    @property
    def value(self) -> float | None:
        return self.numerator / self.denominator * 100 if self.denominator else None

    def display(self) -> str:
        if self.value is None:
            return f'{self.name}尚不能計算（分母為0）'
        return f'{self.name}{self.value:.1f}%（{self.numerator}/{self.denominator}）'


def metrics(question: str, history=None) -> dict[str, Metric]:
    counts = stage_counts(question, history)
    return {name: Metric(name, counts[num], counts[den]) for name, (num, den) in RATE_STAGES.items()
            if num in counts and den in counts}


def is_deliverable(question: str, history=None) -> bool:
    return bool(DELIVERABLE.search(question) or (
        re.search(r'縮短|保留|再短|改成|自然一點', question)
        and any(DELIVERABLE.search(text) for text in user_texts('', history))))


@dataclass(frozen=True)
class KnownFact:
    kind: str
    value: str
    polarity: str
    order: tuple[int, int, int]


def _fact_value(kind: str, value: str) -> str:
    normalized = re.sub(r'[\s,，]', '', value).replace('：', ':')
    if kind == 'price':
        return str(int(re.search(r'\d+', normalized)[0]))
    return normalized.replace('星期', '週').replace('禮拜', '週').replace('本週', '這週')


def _polarity(text: str, start: int, end: int) -> str:
    following = text[end:end + 14]
    preceding = text[max(0, start - 5):start]
    if re.match(r'\s*(?:沒有空|沒空|沒位|沒有位|不方便|不能約|不能來|已滿|滿了|不行|不可以)', following) or re.search(r'(?:不是|不要|不能約)\s*$', preceding):
        return 'negative'
    if re.match(r'\s*(?:還)?(?:有空|有位|有空檔|可以約|能約|可以來)', following):
        return 'positive'
    return 'specified'


def known_facts(question: str, history=None) -> dict[tuple[str, str], KnownFact]:
    facts = {}
    latest_price = None
    for turn, text in enumerate(user_texts(question, history)):
        for match in SPECIFIC.finditer(text):
            kind = match.lastgroup
            # Ambiguous quantity wording is a price candidate in output, not money evidence.
            if kind == 'price' and re.match(r'只要|總共', match[0]):
                context = text[max(0, match.start() - 12):match.end()]
                if not re.search(r'價格|報價|費用|收費|元|塊|\$', context):
                    continue
            value = _fact_value(kind, match[0])
            fact = KnownFact(kind, value, _polarity(text, match.start(), match.end()),
                             (_currentness(text, match.start()), turn, match.start()))
            if kind == 'price':
                if latest_price is None or fact.order >= latest_price.order:
                    latest_price = fact
            elif (kind, value) not in facts or fact.order >= facts[kind, value].order:
                facts[kind, value] = fact
    if latest_price:
        facts['price', latest_price.value] = latest_price
    return facts


def _authorized(match, answer: str, facts) -> bool:
    kind = match.lastgroup
    fact = facts.get((kind, _fact_value(kind, match[0])))
    if fact is None:
        return False
    if kind == 'date_time':
        claim = _polarity(answer, match.start(), match.end())
        if fact.polarity == 'negative':
            return claim == 'negative'
        if claim == 'positive':
            return fact.polarity == 'positive'
        if claim == 'negative':
            return False
    return fact.polarity != 'negative'


def inspect(question: str, answer: str, history=None) -> tuple[str, list[str]]:
    """Correct only verifiable mismatches; report reasons usable by the bounded retry."""
    available = metrics(question, history)
    counts = stage_counts(question, history)
    found = []
    def rate(match):
        name = match['name']
        metric = available.get(name)
        if metric and metric.value is not None and abs(metric.value - float(match['value'])) <= .11:
            return match[0]
        # No user funnel counts means this could be a sourced benchmark, checked elsewhere.
        if not counts:
            return match[0]
        found.append(f'漏斗指標「{name}」的分子、分母或數值不符使用者資料；不可推測缺少的階段。')
        booking = available.get('私訊轉預約率')
        if '到店' in name and 'attendance' not in counts and booking:
            return booking.display() + '；尚無到店人數，不能計算到店率'
        return metric.display() if metric else f'{name}尚不能計算（缺少對應人數）'
    corrected = RATE.sub(rate, answer)
    if is_deliverable(question, history):
        known = '\n'.join(user_texts(question, history))
        facts = known_facts(question, history)
        def specific(match):
            if _authorized(match, corrected, facts):
                return match[0]
            found.append(f'客用成品有未提供的具體資訊「{match[0]}」，請改為佔位符。')
            if match.lastgroup == 'price':
                return '〔價格待確認〕'
            if match.lastgroup == 'store':
                return '〔店家資訊待確認〕'
            return '〔日期／時段待確認〕'
        corrected = SPECIFIC.sub(specific, corrected)
        # Unknown foreign script at the end is suspicious, but user-provided names survive.
        tail = re.search(r'(?P<junk>[\u0370-\u052f\u3040-\u30ff\u0900-\u0dff]+)(?P<end>\s*(?:\[\d+\])?\s*)$', corrected)
        if not tail:
            tail = re.search(r'(?:告知|通知|謝謝|見面|再約)\s+(?P<junk>[\u4e00-\u9fff]{2,4})(?P<end>\s*(?:\[\d+\])?\s*)$', corrected)
        courtesy = ('謝謝配合', '感謝配合', '謝謝你', '謝謝您', '感謝你', '感謝您', '謝謝', '感謝')
        if tail and tail['junk'] not in courtesy and tail['junk'] not in known and not re.search(r'署名|名字|簽名', question):
            found.append('客用成品結尾有來源不明的異常字串，請保留正文並移除異常尾碼。')
            corrected = corrected[:tail.start('junk')] + corrected[tail.end('junk'):]
        if re.search(r"提醒|保留", question):
            for value in re.findall(r"\d{1,2}[:：]\d{2}", question):
                if value.replace("：", ":") not in corrected.replace("：", ":"):
                    found.append(f"提醒必須保留使用者提供的時間{value}。")
                    corrected += f"\n預約時間 {value}"
            if "遲到" in question and re.search(r"告知|通知", question) and not (
                    "遲到" in corrected and re.search(r"告知|通知", corrected)):
                found.append("提醒必須保留遲到事先告知的要求。")
                corrected += "\n若會遲到請先告知"
    return corrected.strip(), found


def failure_reply(question: str, history=None) -> str:
    if is_deliverable(question, history):
        return '這段話剛剛沒有寫完整\n請重送一次 我會保留你給的資訊再寫'
    return '這題剛剛沒有整理完整\n請重送一次 我會接著你這個問題回答'
