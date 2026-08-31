import re
import unicodedata


CJK_SEQUENCE = re.compile(r"[\u3400-\u9fff]+")
LATIN_WORD = re.compile(r"[a-z0-9]+")


def normalize_for_search(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(text or "")).lower()
    return "".join(char if (char.isalnum() or "\u3400" <= char <= "\u9fff") else " " for char in normalized)


def search_tokens(text: str) -> list[str]:
    normalized = normalize_for_search(text)
    tokens: list[str] = LATIN_WORD.findall(normalized)
    for sequence in CJK_SEQUENCE.findall(normalized):
        if len(sequence) == 1:
            tokens.append(sequence)
            continue
        for size in (2, 3):
            tokens.extend(sequence[index:index + size] for index in range(len(sequence) - size + 1))
        if len(sequence) <= 8:
            tokens.append(sequence)
    return list(dict.fromkeys(token for token in tokens if token))


def cjk_bigrams(text: str) -> set[str]:
    normalized = normalize_for_search(text)
    return {
        sequence[index:index + 2]
        for sequence in CJK_SEQUENCE.findall(normalized)
        for index in range(len(sequence) - 1)
    }


def fts_query(text: str) -> str:
    tokens = search_tokens(text)
    return " OR ".join(f'"{token}"' for token in tokens)
