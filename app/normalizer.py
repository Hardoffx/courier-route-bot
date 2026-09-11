import re


def normalize_address(text: str) -> str:
    s = text.replace("\n", " ").strip()
    s = s.replace("№", " ").replace("Nº", " ").replace("No", " ")
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s*,\s*", ", ", s)
    s = re.sub(r"\bул\.\s*", "ул ", s, flags=re.I)
    s = re.sub(r"\bпер\.\s*", "пер ", s, flags=re.I)
    s = re.sub(r"\bш\.\s*", "ш ", s, flags=re.I)
    s = re.sub(r"\bд\.\s*д\.\s*", "", s, flags=re.I)
    s = re.sub(r"\bдом\s*", "", s, flags=re.I)
    s = re.sub(r"\bд\.\s*", "", s, flags=re.I)
    s = re.sub(r",?\s*корпус\s*(\d+[А-Яа-яA-Za-z]?)", r"к\1", s, flags=re.I)
    s = re.sub(r",?\s*к\.\s*(\d+[А-Яа-яA-Za-z]?)", r"к\1", s, flags=re.I)
    s = re.sub(r",?\s*стр\.\s*(\d+[А-Яа-яA-Za-z]?)", r" стр. \1", s, flags=re.I)
    s = re.sub(r"\b([А-ЯЁ][А-Яа-яЁё\-\s]+?)\s+ул\b", r"ул \1", s)
    s = re.sub(r",\s*(\d+)", r" \1", s)
    s = re.sub(r"(\d+)\s+к(\d+)", r"\1к\2", s)
    s = re.sub(r"\s+,", ",", s)
    s = re.sub(r",\s*,+", ", ", s)
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip(" ,")


def canonical_key(text: str) -> str:
    s = normalize_address(text).lower().replace("ё", "е")
    s = re.sub(r"[.,]", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def house_signature(text: str) -> tuple[str, ...]:
    s = canonical_key(text)
    nums = re.findall(r"(?<!\w)\d+[а-яa-z]?(?:к\d+)?", s)
    return tuple(nums[-2:])
