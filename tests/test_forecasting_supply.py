"""printaudit.forecasting.supply — прогноз даты исчерпания расходника по
линейному тренду, со сбросом на замену картриджа."""
from datetime import date, timedelta

from printaudit.forecasting.supply import SupplyTrendPoint, estimate_exhaustion


def _points(start: date, levels):
    return [SupplyTrendPoint(day=start + timedelta(days=i), level_percent=lvl) for i, lvl in enumerate(levels)]


def test_steady_decline_produces_exhaustion_date():
    start = date(2026, 1, 1)
    points = _points(start, [80, 70, 60, 50, 40, 30])  # -10%/день
    result = estimate_exhaustion(points, today=start + timedelta(days=5))
    assert result is not None
    assert result["slope_percent_per_day"] < 0
    days_to_empty = (result["exhaustion_date"] - (start + timedelta(days=5))).days
    assert 2 <= days_to_empty <= 4  # ~30/10=3 дня


def test_cartridge_replacement_resets_trend_window():
    start = date(2026, 1, 1)
    # Убывание, потом скачок вверх (замена), потом новое убывание.
    points = _points(start, [30, 20, 10, 95, 90, 85, 80, 75])
    result = estimate_exhaustion(points, today=start + timedelta(days=7))
    assert result is not None
    # Оценка должна использовать ТОЛЬКО точки после замены (75..95), а не
    # смешивать со старым отрезком (иначе наклон/уровень были бы другими).
    assert result["current_level_percent"] == 75


def test_increasing_level_returns_none_not_fabricated_date():
    start = date(2026, 1, 1)
    points = _points(start, [10, 20, 30, 40, 50])
    assert estimate_exhaustion(points, today=start + timedelta(days=4)) is None


def test_flat_level_returns_none():
    start = date(2026, 1, 1)
    points = _points(start, [50, 50, 50, 50, 50])
    assert estimate_exhaustion(points, today=start + timedelta(days=4)) is None


def test_too_few_points_returns_none():
    start = date(2026, 1, 1)
    points = _points(start, [50, 40])
    assert estimate_exhaustion(points, today=start + timedelta(days=1)) is None


def test_all_points_same_day_returns_none():
    start = date(2026, 1, 1)
    points = [SupplyTrendPoint(day=start, level_percent=lvl) for lvl in [50, 40, 30, 20, 10]]
    assert estimate_exhaustion(points, today=start) is None


def test_empty_points_returns_none():
    assert estimate_exhaustion([]) is None


def test_already_at_or_below_zero_returns_none():
    start = date(2026, 1, 1)
    points = _points(start, [10, 8, 6, 4, 0])
    assert estimate_exhaustion(points, today=start + timedelta(days=4)) is None
