"""Прогнозирование нагрузки/расходников/простоев (Part 7,
docs/PRINTER_MONITORING_FORECASTING.md).

Философия намеренно та же, что и у остального мониторинга ("не выдумывать
точность, которой нет"): простые, объяснимые baseline-модели (не
"чёрный ящик"), автоматический выбор лучшей по обратной проверке
(backtest), явный результат "недостаточно данных" вместо фиктивного
прогноза, когда истории не хватает."""

HORIZON_DAYS = (7, 30, 90)

# Минимальная история, необходимая для прогноза на данный горизонт —
# РОВНО вдвое длиннее горизонта: printaudit.forecasting.forecast.build_forecast
# использует min_train_size=horizon_days для backtest, поэтому 2*horizon —
# это минимум, при котором rolling-origin backtest в принципе может
# набрать хотя бы ОДНУ проверочную точку (train=horizon_days,
# actual=следующие horizon_days). Задокументировано и протестировано (см.
# tests/test_forecasting_forecast.py, test_forecasting_pipeline.py).
MIN_HISTORY_DAYS = {7: 14, 30: 60, 90: 180}

SCOPE_DEVICE = "device"
SCOPE_QUEUE = "queue"
SCOPE_SITE = "site"
SCOPE_ORGANIZATION = "organization"
SCOPES = (SCOPE_DEVICE, SCOPE_QUEUE, SCOPE_SITE, SCOPE_ORGANIZATION)

METRIC_JOB_COUNT = "job_count"
METRIC_TOTAL_PAGES = "total_pages"
METRIC_COLOR_PAGES = "color_pages"
METRIC_BW_PAGES = "bw_pages"
METRIC_COST = "cost"
LOAD_METRICS = (METRIC_JOB_COUNT, METRIC_TOTAL_PAGES, METRIC_COLOR_PAGES, METRIC_BW_PAGES, METRIC_COST)

METRIC_TONER_EXHAUSTION = "toner_exhaustion"
METRIC_DOWNTIME_RISK = "downtime_risk"

MODEL_SEASONAL_NAIVE = "seasonal_naive"
MODEL_MOVING_AVERAGE = "moving_average"
MODEL_EXPONENTIAL_SMOOTHING = "exponential_smoothing"

FORECAST_MODEL_VERSION = "1.0"


def min_history_days(horizon_days: int) -> int:
    return MIN_HISTORY_DAYS.get(horizon_days, horizon_days * 2)
