"""Прогноз даты исчерпания расходника (тонер/картридж) по тренду уровня —
простая линейная регрессия по точкам ПОСЛЕ последней обнаруженной замены
картриджа (см. `_find_last_reset`), а не по всей истории: замена сбрасывает
уровень вверх и старые точки до замены не имеют отношения к текущему
расходу.

MVP-эвристика, не модель временных рядов — уровень тонера обычно убывает
почти линейно между заменами, задача не оправдывает более сложной модели.
Явно возвращает None (а не 0/выдуманную дату), когда тренд не убывает
(долив/замена, недостаточно точек) или расходник уже в состоянии unknown."""
from dataclasses import dataclass
from datetime import date, timedelta
from typing import List, Optional, Tuple

MIN_POINTS_FOR_TREND = 5
MIN_SPAN_DAYS_FOR_TREND = 3
# Скачок уровня вверх больше этого порога трактуется как замена картриджа
# (сброс тренда), а не шум измерения.
RESET_JUMP_THRESHOLD_PERCENT = 10.0


@dataclass
class SupplyTrendPoint:
    day: date
    level_percent: float


def _find_last_reset_index(points: List[SupplyTrendPoint]) -> int:
    """Индекс, с которого начинается текущий (после последней замены)
    отрезок тренда — 0, если замен в истории не обнаружено."""
    last_reset = 0
    for i in range(1, len(points)):
        if points[i].level_percent - points[i - 1].level_percent > RESET_JUMP_THRESHOLD_PERCENT:
            last_reset = i
    return last_reset


def _linear_slope_per_day(points: List[SupplyTrendPoint]) -> Optional[float]:
    """Наклон level_percent/день методом наименьших квадратов. None, если
    все точки в один день (нет временнОй протяжённости)."""
    if len(points) < 2:
        return None
    base_day = points[0].day
    xs = [(p.day - base_day).days for p in points]
    ys = [p.level_percent for p in points]
    n = len(points)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator == 0:
        return None
    return numerator / denominator


def estimate_exhaustion(points: List[SupplyTrendPoint], today: Optional[date] = None) -> Optional[dict]:
    """points — история одного supply_type ОДНОГО устройства, отсортированная
    по возрастанию дня, level_percent НЕ None (unknown-точки должны быть
    отфильтрованы вызывающим кодом ДО передачи сюда — см.
    printaudit/forecasting/pipeline.py).

    Возвращает {"exhaustion_date": date, "slope_percent_per_day": float,
    "current_level_percent": float} или None, если оценить нельзя
    (недостаточно точек/протяжённости, уровень не убывает, уже <= 0)."""
    if not points:
        return None
    today = today or points[-1].day

    reset_idx = _find_last_reset_index(points)
    trend_points = points[reset_idx:]
    if len(trend_points) < MIN_POINTS_FOR_TREND:
        return None
    span_days = (trend_points[-1].day - trend_points[0].day).days
    if span_days < MIN_SPAN_DAYS_FOR_TREND:
        return None

    slope = _linear_slope_per_day(trend_points)
    if slope is None or slope >= 0:
        # Не убывает (долив/шум/плато) -- не выдумываем дату исчерпания.
        return None

    current_level = trend_points[-1].level_percent
    if current_level <= 0:
        return None

    days_to_empty = current_level / (-slope)
    exhaustion_date = today + timedelta(days=days_to_empty)
    return {
        "exhaustion_date": exhaustion_date,
        "slope_percent_per_day": slope,
        "current_level_percent": current_level,
    }
