"""Эвристики риска простоя и аномального изменения нагрузки — намеренно
простые (доля недоступности + число активных алертов; отклонение факта
от базового прогноза), не ML-модель (см. printaudit/forecasting/__init__.py
про философию "не чёрный ящик"). Возвращают явные категории/числа с
понятным объяснением, а не непрозрачную "вероятность", которую нечем
обосновать при таком объёме истории."""
from dataclasses import dataclass
from typing import List, Optional

RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"

# Доля health-сэмплов за период с is_reachable=False или device_status in
# (error, offline) -- пороги сознательно консервативны для MVP.
UNAVAILABLE_RATIO_MEDIUM = 0.05
UNAVAILABLE_RATIO_HIGH = 0.20
ACTIVE_ERROR_ALERTS_HIGH = 1

# Отклонение факта периода от прогноза (в долях от прогноза) считается
# аномальным, если выходит за это значение -- используется вместе с
# доверительным интервалом прогноза, если он есть.
ANOMALY_DEVIATION_RATIO = 0.5


@dataclass
class DowntimeRisk:
    level: str
    unavailable_ratio: float
    active_error_alert_count: int
    sample_count: int


def estimate_downtime_risk(
    unavailable_samples: int, total_samples: int, active_error_alert_count: int,
) -> Optional[DowntimeRisk]:
    if total_samples == 0:
        return None
    ratio = unavailable_samples / total_samples
    if active_error_alert_count >= ACTIVE_ERROR_ALERTS_HIGH or ratio >= UNAVAILABLE_RATIO_HIGH:
        level = RISK_HIGH
    elif ratio >= UNAVAILABLE_RATIO_MEDIUM:
        level = RISK_MEDIUM
    else:
        level = RISK_LOW
    return DowntimeRisk(
        level=level, unavailable_ratio=ratio, active_error_alert_count=active_error_alert_count,
        sample_count=total_samples,
    )


def is_anomalous_load(actual_value: float, baseline_value: float) -> bool:
    """baseline_value — точка прогноза (или предыдущего периода) для того
    же метрики/периода. Ноль-базовая линия (ранее не печатали вовсе) не
    считается аномалией сама по себе — избегаем деления на ноль и ложных
    срабатываний на новых устройствах."""
    if baseline_value <= 0:
        return False
    deviation = abs(actual_value - baseline_value) / baseline_value
    return deviation >= ANOMALY_DEVIATION_RATIO
