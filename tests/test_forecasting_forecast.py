"""printaudit.forecasting.forecast.build_forecast — сборка итогового
прогноза (модель + доверительный интервал) или явное "недостаточно
данных"."""
from printaudit.forecasting import MIN_HISTORY_DAYS
from printaudit.forecasting.forecast import build_forecast


def test_insufficient_history_returns_flag_without_fabricated_values():
    history = [10.0] * (MIN_HISTORY_DAYS[7] - 1)
    result = build_forecast(history, horizon_days=7)
    assert result.insufficient_history is True
    assert result.values == []
    assert result.model_name is None


def test_sufficient_history_produces_forecast_with_confidence_interval():
    history = ([10, 20, 30, 15, 25, 5, 8] * 4)  # ровно MIN_HISTORY_DAYS[7] = 14 дней? нет, 28
    result = build_forecast(history, horizon_days=7)
    assert result.insufficient_history is False
    assert len(result.values) == 7
    assert result.model_name is not None
    assert len(result.ci_lower) == 7
    assert len(result.ci_upper) == 7
    assert all(lo <= v <= hi for lo, v, hi in zip(result.ci_lower, result.values, result.ci_upper))


def test_confidence_interval_lower_bound_never_negative():
    history = [0.0] * 30
    result = build_forecast(history, horizon_days=7)
    assert all(v >= 0 for v in result.ci_lower)


def test_exact_minimum_history_boundary_is_sufficient():
    history = [5.0] * MIN_HISTORY_DAYS[7]
    result = build_forecast(history, horizon_days=7)
    assert result.insufficient_history is False


def test_one_day_short_of_minimum_is_insufficient():
    history = [5.0] * (MIN_HISTORY_DAYS[7] - 1)
    result = build_forecast(history, horizon_days=7)
    assert result.insufficient_history is True


def test_to_json_dict_is_json_serializable():
    import json

    history = ([10, 20, 30, 15, 25, 5, 8] * 4)
    result = build_forecast(history, horizon_days=7)
    json.dumps(result.to_json_dict())  # не должно бросить исключение
