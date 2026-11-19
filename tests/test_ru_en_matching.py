"""Russian ↔ English participant matching on real pairs seen on KZ bookmakers (19.11.2026)."""

import pytest

from app.normalization.names import name_variants, similarity
from app.providers.bookmakers.common.textutil import cyber_sport


def best(a: str, b: str, person: bool) -> float:
    return max(similarity(x, y, person=person) for x in name_variants(a) for y in name_variants(b))


@pytest.mark.parametrize("a, b, person", [
    ("Rostov", "Ростов", False), ("Krylia Sovetov", "Крылья Советов", False), ("Akron Tolyatti", "Акрон", False),
    ("Orenburg", "Оренбург", False), ("Spartak Moscow", "Спартак Москва", False),
    ("Dynamo Moscow", "Динамо Москва", False), ("Juventus", "Ювентус", False), ("Bayern Munich", "Бавария", False),
    ("Ben Shelton", "Шелтон Бен", True), ("Shelton B", "Шелтон Б.", True), ("Rafael Jodar", "Ходар Рафаэль", True),
    ("Alexander Zverev", "Зверев А.", True),
])
def test_same_participant(a, b, person):
    assert best(a, b, person) >= 0.9


@pytest.mark.parametrize("a, b, person", [
    ("Hertha Berlin", "Герта Берлин (люб)", False),  # amateur side ≠ first team
    ("Spartak Moscow", "Спартак Тамбов", False), ("Dinamo Moscow", "Динамо Минск", False),
    ("Dinamo", "Динамо Минск", False), ("Lokomotiv Moscow", "Локомотив София", False),
    ("Real Madrid", "Реал Сосьедад", False), ("Rostov", "Рубин", False),
    ("Andy Murray", "Andy Murphy", True), ("Andy Murray", "Мерфи Энди", True), ("Shelton B", "Шелтон Т.", True),
])
def test_different_participant(a, b, person):
    assert best(a, b, person) < 0.9


def test_cyber_football_is_its_own_sport():
    assert cyber_sport("football", "Наполи (Rodja)", "Рома (Bjela)") == "efootball"
    assert cyber_sport("football", "Герта Берлин (люб)", "Динамо Дрезден (люб)") == "football"
    assert cyber_sport("football", "Аль-Талаба (до21)", "Аль-Наджаф (до21)") == "football"
