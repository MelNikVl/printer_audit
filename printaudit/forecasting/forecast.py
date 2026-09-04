"""Собирает результат backtest+выбор модели в готовый прогноз с
доверительным интервалом — то, что в итоге попадает в
PrinterAlert... то есть ForecastRun.forecast_json (см.
printaudit/forecasting/pipeline.py).

Доверительный интервал — НАМЕРЕННО приближённый (± z*MAE остатков
backtest, нормальное приближение), а не строгий статистический интервал:
честно назван "approximate" в самой структуре результата, чтобы не
выдавать MVP-оценку за точную статистику (см. требование "не выдумывать
точность, которой нет")."""
from dataclasses import dataclass, field
from typing import List, Optional

from printaudit.forecasting import min_history_days
from printaudit.forecasting.backtest import select_best_model
from printaudit.forecasting.models import MODEL_FUNCTIONS

CONFIDENCE_Z = 1.96  # ~95%, приближённо (нормальное распределение остатков)


@dataclass
class ForecastResult:
    insufficient_history: bool
    history_days_used: int
    model_name: Optional[str] = None
    mae: Optional[float] = None
    wape: Optional[float] = None
    values: List[float] = field(default_factory=list)
    ci_lower: List[float] = field(default_factory=list)
    ci_upper: List[float] = field(default_factory=list)

    def to_json_dict(self) -> dict:
        return {
            "insufficient_history": self.insufficient_history,
            "history_days_used": self.history_days_used,
            "model_name": self.model_name,
            "mae": self.mae,
            "wape": self.wape,
            "values": self.values,
            "ci_lower": self.ci_lower,
            "ci_upper": self.ci_upper,
            "confidence_interval_method": "approximate_normal_backtest_residual" if self.model_name else None,
        }


def build_forecast(history: List[float], horizon_days: int) -> ForecastResult:
    """history — плотный ежедневный ряд (см. printaudit/forecasting/series.py),
    заканчивающийся вчерашним/сегодняшним днём. Минимальная длина истории —
    printaudit.forecasting.min_history_days(horizon_days), задокументирована
    и одинакова для решения "есть ли смысл вообще пытаться" и для
    min_train_size backtest."""
    required = min_history_days(horizon_days)
    history_days_used = len(history)
    if history_days_used < required:
        return ForecastResult(insufficient_history=True, history_days_used=history_days_used)

    best = select_best_model(history, horizon_days, min_train_size=horizon_days)
    if best is None:
        return ForecastResult(insufficient_history=True, history_days_used=history_days_used)

    model_fn = MODEL_FUNCTIONS[best.model_name]
    values = model_fn(history, horizon_days)
    margin = CONFIDENCE_Z * best.mae
    ci_lower = [max(0.0, v - margin) for v in values]
    ci_upper = [v + margin for v in values]

    return ForecastResult(
        insufficient_history=False, history_days_used=history_days_used, model_name=best.model_name,
        mae=best.mae, wape=best.wape, values=values, ci_lower=ci_lower, ci_upper=ci_upper,
    )
