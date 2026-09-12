from app.domain.models import AnalysisResult

# Веса детерминированной формулы приоритета — единственное место изменения.
# LLM факторы не ранжирует: итоговый score считает только код (PRODUCT_SPEC §37).
WEIGHTS: dict[str, float] = {
    "goal_fit": 0.30,
    "importance": 0.20,
    "urgency": 0.15,
    "long_term_value": 0.15,
    "interest_fit": 0.10,
    "quick_win": 0.10,
}


def quick_win(estimated_action_minutes: int | None) -> float:
    """Быстрые победы считаются кодом из оценки длительности, не моделью."""
    if estimated_action_minutes is None:
        return 0.5
    return max(0.0, 1.0 - estimated_action_minutes / 60)


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


class PriorityEngine:
    def score(self, analysis: AnalysisResult) -> int:
        factors = {
            "goal_fit": analysis.goal_fit,
            "importance": analysis.importance,
            "urgency": analysis.urgency,
            "long_term_value": analysis.long_term_value,
            "interest_fit": analysis.interest_fit,
            "quick_win": quick_win(analysis.estimated_action_minutes),
        }
        total = sum(WEIGHTS[name] * clamp01(value) for name, value in factors.items())
        return round(clamp01(total) * 100)
