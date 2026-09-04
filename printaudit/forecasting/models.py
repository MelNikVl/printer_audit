"""Baseline-модели прогноза по плотному ежедневному ряду (список float,
без пропусков — дни без заданий представлены явным 0.0, см.
printaudit/forecasting/series.py). Каждая модель — чистая функция
`(history: List[float], horizon: int) -> List[float]`, без побочных
эффектов и без обращения к БД — тестируется на синтетических рядах."""
from typing import List

SEASON_LENGTH_DEFAULT = 7


def seasonal_naive_forecast(history: List[float], horizon: int, season_length: int = SEASON_LENGTH_DEFAULT) -> List[float]:
    """Прогноз = значение того же дня недели (сезона) `season_length` дней
    назад от текущей прогнозируемой точки, беря последний известный сезон
    и продолжая его циклически. Требует history длиной >= season_length —
    вызывающий код (select_best_model) обязан это проверить сам."""
    if len(history) < season_length:
        raise ValueError(f"history короче season_length={season_length}")
    last_season = history[-season_length:]
    return [last_season[i % season_length] for i in range(horizon)]


def moving_average_forecast(history: List[float], horizon: int, window: int = SEASON_LENGTH_DEFAULT) -> List[float]:
    """Плоский прогноз = среднее последних `window` значений истории,
    повторённое на весь горизонт. Простейшая модель без учёта сезонности —
    baseline для сравнения."""
    if not history:
        raise ValueError("history пуста")
    tail = history[-window:] if len(history) >= window else history
    avg = sum(tail) / len(tail)
    return [avg] * horizon


def exponential_smoothing_forecast(history: List[float], horizon: int, alpha: float = 0.3) -> List[float]:
    """Простое экспоненциальное сглаживание (SES) — плоский прогноз, равный
    последнему сглаженному уровню. alpha=0.3 — типичное умеренное сглаживание,
    без автоподбора (не переусложняем MVP)."""
    if not history:
        raise ValueError("history пуста")
    level = history[0]
    for value in history[1:]:
        level = alpha * value + (1 - alpha) * level
    return [level] * horizon


MODEL_FUNCTIONS = {
    "seasonal_naive": seasonal_naive_forecast,
    "moving_average": moving_average_forecast,
    "exponential_smoothing": exponential_smoothing_forecast,
}
