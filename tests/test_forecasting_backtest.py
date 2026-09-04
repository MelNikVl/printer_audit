"""printaudit.forecasting.backtest — rolling-origin backtest и выбор
лучшей модели (WAPE), на синтетических рядах."""
from printaudit.forecasting.backtest import rolling_backtest, select_best_model
from printaudit.forecasting.models import moving_average_forecast, seasonal_naive_forecast


def test_rolling_backtest_perfect_seasonal_series_has_zero_error():
    history = ([1, 2, 3, 4, 5, 6, 7] * 6)  # идеально повторяющийся недельный паттерн
    result = rolling_backtest(history, seasonal_naive_forecast, horizon=7, min_train_size=14)
    assert result is not None
    assert result["mae"] == 0.0
    assert result["wape"] == 0.0
    assert result["n_origins"] > 0


def test_rolling_backtest_returns_none_when_not_enough_history():
    result = rolling_backtest([1, 2, 3], moving_average_forecast, horizon=7, min_train_size=14)
    assert result is None


def test_rolling_backtest_wape_none_when_actuals_all_zero():
    history = [0.0] * 30
    result = rolling_backtest(history, moving_average_forecast, horizon=7, min_train_size=14)
    assert result is not None
    assert result["mae"] == 0.0
    assert result["wape"] is None  # неопределён при нулевом фактическом объёме


def test_select_best_model_prefers_seasonal_naive_on_seasonal_series():
    history = ([10, 20, 30, 15, 25, 5, 8] * 8)
    best = select_best_model(history, horizon=7, min_train_size=14)
    assert best is not None
    assert best.model_name == "seasonal_naive"
    assert best.wape == 0.0


def test_select_best_model_returns_none_when_insufficient_history():
    best = select_best_model([1, 2, 3], horizon=30, min_train_size=42)
    assert best is None


def test_select_best_model_picks_moving_average_on_flat_noisy_series():
    import random

    random.seed(42)
    history = [50 + random.uniform(-1, 1) for _ in range(60)]
    best = select_best_model(history, horizon=7, min_train_size=14)
    assert best is not None
    assert best.model_name in ("moving_average", "exponential_smoothing")
