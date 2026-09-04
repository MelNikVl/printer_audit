"""printaudit.forecasting.models — baseline-модели на синтетических рядах,
без БД."""
import pytest

from printaudit.forecasting.models import (
    exponential_smoothing_forecast,
    moving_average_forecast,
    seasonal_naive_forecast,
)


def test_seasonal_naive_repeats_last_week():
    # 2 полные недели, вторая неделя = [10..16]
    history = list(range(1, 8)) + list(range(10, 17))
    forecast = seasonal_naive_forecast(history, horizon=7, season_length=7)
    assert forecast == list(range(10, 17))


def test_seasonal_naive_cycles_beyond_one_season():
    history = [1, 2, 3, 4, 5, 6, 7]
    forecast = seasonal_naive_forecast(history, horizon=14, season_length=7)
    assert forecast == [1, 2, 3, 4, 5, 6, 7, 1, 2, 3, 4, 5, 6, 7]


def test_seasonal_naive_requires_at_least_one_season():
    with pytest.raises(ValueError):
        seasonal_naive_forecast([1, 2, 3], horizon=7, season_length=7)


def test_moving_average_is_flat_mean_of_window():
    history = [10, 20, 30, 40]
    forecast = moving_average_forecast(history, horizon=3, window=4)
    assert forecast == [25.0, 25.0, 25.0]


def test_moving_average_uses_available_history_when_shorter_than_window():
    forecast = moving_average_forecast([10, 20], horizon=2, window=7)
    assert forecast == [15.0, 15.0]


def test_moving_average_requires_nonempty_history():
    with pytest.raises(ValueError):
        moving_average_forecast([], horizon=3)


def test_exponential_smoothing_is_flat_and_bounded_by_history_range():
    history = [10, 20, 10, 20, 10, 20]
    forecast = exponential_smoothing_forecast(history, horizon=5, alpha=0.5)
    assert len(forecast) == 5
    assert all(f == forecast[0] for f in forecast)
    assert 10 <= forecast[0] <= 20


def test_exponential_smoothing_constant_series_converges_to_constant():
    forecast = exponential_smoothing_forecast([42.0] * 10, horizon=3, alpha=0.3)
    assert forecast == [42.0, 42.0, 42.0]


def test_exponential_smoothing_requires_nonempty_history():
    with pytest.raises(ValueError):
        exponential_smoothing_forecast([], horizon=3)
