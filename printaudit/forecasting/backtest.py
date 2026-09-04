"""Rolling-origin backtest и автоматический выбор лучшей baseline-модели
по WAPE (см. printaudit/forecasting/models.py). "Rolling-origin" — не
одна проверка на последних N днях, а несколько: точка отсчёта сдвигается
вперёд по истории с шагом `step`, на каждой модель обучается только на
данных ДО этой точки и сравнивается с фактом после неё — так метрика не
зависит от одного случайно удачного/неудачного отрезка."""
from dataclasses import dataclass
from typing import Callable, List, Optional

from printaudit.forecasting.models import MODEL_FUNCTIONS


@dataclass
class BacktestResult:
    model_name: str
    mae: float
    wape: Optional[float]
    n_origins: int


def rolling_backtest(
    history: List[float], model_fn: Callable[[List[float], int], List[float]],
    horizon: int, min_train_size: int, step: Optional[int] = None,
) -> Optional[dict]:
    """Возвращает {"mae": ..., "wape": ..., "n_origins": ...} или None, если
    в истории недостаточно точек хотя бы для ОДНОЙ проверки (недостаточно
    данных для честного backtest — не то же самое, что "недостаточно данных
    для прогноза вообще", см. printaudit.forecasting.pipeline)."""
    n = len(history)
    step = step or max(1, horizon // 4)
    abs_errors = []
    actual_sum = 0.0
    used = 0
    origin = min_train_size
    while origin + horizon <= n:
        train = history[:origin]
        actual = history[origin:origin + horizon]
        try:
            pred = model_fn(train, horizon)
        except ValueError:
            origin += step
            continue
        for a, p in zip(actual, pred):
            abs_errors.append(abs(a - p))
            actual_sum += abs(a)
        used += 1
        origin += step

    if used == 0 or not abs_errors:
        return None
    mae = sum(abs_errors) / len(abs_errors)
    wape = (sum(abs_errors) / actual_sum) if actual_sum > 0 else None
    return {"mae": mae, "wape": wape, "n_origins": used}


def select_best_model(history: List[float], horizon: int, min_train_size: int) -> Optional[BacktestResult]:
    """Прогоняет backtest для каждой кандидатной модели и выбирает
    победителя по WAPE (меньше — лучше); модели, для которых WAPE не
    определён (все фактические значения периода — нули), сравниваются по
    MAE. Возвращает None, если backtest не смог набрать НИ ОДНОЙ проверочной
    точки ни для одной модели (истории физически не хватает даже с учётом
    min_train_size) — вызывающий код должен трактовать это как
    "недостаточно данных"."""
    results = []
    for name, fn in MODEL_FUNCTIONS.items():
        metrics = rolling_backtest(history, fn, horizon, min_train_size)
        if metrics is not None:
            results.append(BacktestResult(model_name=name, mae=metrics["mae"], wape=metrics["wape"], n_origins=metrics["n_origins"]))

    if not results:
        return None

    def sort_key(r: BacktestResult):
        return (r.wape if r.wape is not None else float("inf"), r.mae)

    results.sort(key=sort_key)
    return results[0]
