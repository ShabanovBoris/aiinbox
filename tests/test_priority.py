import pytest

from app.domain.models import AnalysisResult
from app.domain.priority import PriorityEngine, quick_win
from tests.fakes import make_analysis


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        # Все факторы по максимуму, действие мгновенное → 100
        (
            {
                "importance": 1.0,
                "urgency": 1.0,
                "goal_fit": 1.0,
                "long_term_value": 1.0,
                "interest_fit": 1.0,
                "estimated_action_minutes": 0,
            },
            100,
        ),
        # Все максимумы, но оценка времени неизвестна → quick_win 0.5 → 95
        (
            {
                "importance": 1.0,
                "urgency": 1.0,
                "goal_fit": 1.0,
                "long_term_value": 1.0,
                "interest_fit": 1.0,
                "estimated_action_minutes": None,
            },
            95,
        ),
        # Все нули, время неизвестно → только quick_win 0.05 → 5
        (
            {
                "importance": 0.0,
                "urgency": 0.0,
                "goal_fit": 0.0,
                "long_term_value": 0.0,
                "interest_fit": 0.0,
                "estimated_action_minutes": None,
            },
            5,
        ),
        # Все нули, действие на 2 часа → quick_win 0 (клэмп отрицательного) → 0
        (
            {
                "importance": 0.0,
                "urgency": 0.0,
                "goal_fit": 0.0,
                "long_term_value": 0.0,
                "interest_fit": 0.0,
                "estimated_action_minutes": 120,
            },
            0,
        ),
        # Смешанный случай: goal и importance в максимумах, остальное нули, час работы
        (
            {
                "importance": 1.0,
                "urgency": 0.0,
                "goal_fit": 1.0,
                "long_term_value": 0.0,
                "interest_fit": 0.0,
                "estimated_action_minutes": 60,
            },
            50,
        ),
    ],
)
def test_priority_engine_exact_values(overrides, expected):
    analysis: AnalysisResult = make_analysis(**overrides)
    assert PriorityEngine().score(analysis) == expected


def test_quick_win():
    assert quick_win(None) == 0.5
    assert quick_win(0) == 1.0
    assert quick_win(60) == 0.0
    assert quick_win(120) == 0.0  # клэмп: отрицательное не допускается
