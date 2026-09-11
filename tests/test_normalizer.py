from app.normalizer import canonical_key, normalize_address


def test_examples_from_real_route_sheet():
    assert normalize_address("Москва г, ул Генерала Белобородова, д. 19, к. 1") == "Москва г, ул Генерала Белобородова 19к1"
    assert normalize_address("Москва г, Дубравная ул, дом Nº 46") == "Москва г, ул Дубравная 46"
    assert normalize_address("Москва г, Митинский 3-й пер, дом Nº 4, корпус 1") == "Москва г, Митинский 3-й пер 4к1"
    assert normalize_address("Москва, ул. Митинская, д. д. 57") == "Москва, ул. Митинская 57"
    assert normalize_address("Москва г, ул Вишнёвая, д. 13, к. 1, стр. 1") == "Москва г, ул Вишнёвая 13к1 стр. 1"


def test_canonical_ignores_cosmetic_punctuation():
    a = canonical_key("Москва г, ул Митинская, д. 27")
    b = canonical_key("Москва г, ул Митинская 27")
    assert a == b
