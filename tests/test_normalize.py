from app.normalize import name_address_fingerprint, normalize_phone, normalize_text, normalize_website, website_domain


def test_normalization() -> None:
    assert normalize_text('ООО "Ёлочка"') == "елочка"
    assert normalize_phone("+7 (999) 123-45-67") == "79991234567"
    assert normalize_phone("8 999 123 45 67") == "79991234567"
    assert website_domain("https://www.Example.COM/path") == "example.com"
    assert normalize_website("https://www.Example.RU/?yclid=123") == "https://example.ru"
    assert normalize_website("example.ru/") == "https://example.ru"
    assert normalize_website("https://yandex.ru/maps/org/test") is None
    assert normalize_website("https://2gis.ru/moscow/firm/1") is None
    assert name_address_fingerprint("ООО Ёлочка", "ул. Ленина, 1") == name_address_fingerprint(
        "елочка", "ул Ленина 1"
    )
