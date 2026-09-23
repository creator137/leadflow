"""A small, deterministic city catalog used by regional searches.

The parser adapters search a concrete city, not a whole administrative subject.
Keeping the expansion on our side makes a regional run transparent, repeatable
and lets its single new-company limit and deduplication work as intended.
"""
from __future__ import annotations

# The first supported region is intentionally explicit rather than delegated to
# a third-party geocoder at runtime. It covers all cities of Moscow Oblast and
# can be extended with more regions without changing the search pipeline.
MOSCOW_OBLAST_CITIES: tuple[str, ...] = (
    "Апрелевка", "Балашиха", "Белоозёрский", "Бронницы", "Верея", "Видное",
    "Волоколамск", "Воскресенск", "Высоковск", "Голицыно", "Дедовск", "Дзержинский",
    "Дмитров", "Долгопрудный", "Домодедово", "Дрезна", "Дубна", "Егорьевск",
    "Жуковский", "Зарайск", "Звенигород", "Ивантеевка", "Истра", "Кашира", "Клин",
    "Коломна", "Королёв", "Котельники", "Красноармейск", "Красногорск", "Краснозаводск",
    "Краснознаменск", "Кубинка", "Куровское", "Ликино-Дулёво", "Лобня",
    "Лосино-Петровский", "Луховицы", "Лыткарино", "Люберцы", "Можайск", "Мытищи",
    "Наро-Фоминск", "Ногинск", "Одинцово", "Озёры", "Орехово-Зуево", "Павловский Посад",
    "Пересвет", "Подольск", "Протвино", "Пушкино", "Пущино", "Раменское", "Реутов",
    "Рошаль", "Руза", "Сергиев Посад", "Серпухов", "Солнечногорск", "Старая Купавна",
    "Ступино", "Талдом", "Фрязино", "Химки", "Хотьково", "Черноголовка", "Чехов",
    "Шатура", "Щёлково", "Электрогорск", "Электросталь", "Электроугли", "Яхрома",
)

REGION_CITIES: dict[str, tuple[str, ...]] = {
    "Московская область": MOSCOW_OBLAST_CITIES,
}


def cities_for_region(region: str) -> tuple[str, ...]:
    return REGION_CITIES.get(region.strip(), ())


def available_regions() -> list[dict[str, object]]:
    return [
        {"name": name, "cities_count": len(cities)}
        for name, cities in sorted(REGION_CITIES.items())
    ]
