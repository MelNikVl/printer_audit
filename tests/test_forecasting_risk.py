"""printaudit.forecasting.risk — эвристики риска простоя и аномальной
нагрузки."""
from printaudit.forecasting.risk import RISK_HIGH, RISK_LOW, RISK_MEDIUM, estimate_downtime_risk, is_anomalous_load


def test_no_samples_returns_none():
    assert estimate_downtime_risk(0, 0, 0) is None


def test_fully_available_no_errors_is_low_risk():
    risk = estimate_downtime_risk(unavailable_samples=0, total_samples=100, active_error_alert_count=0)
    assert risk.level == RISK_LOW


def test_moderate_unavailability_is_medium_risk():
    risk = estimate_downtime_risk(unavailable_samples=10, total_samples=100, active_error_alert_count=0)
    assert risk.level == RISK_MEDIUM


def test_high_unavailability_is_high_risk():
    risk = estimate_downtime_risk(unavailable_samples=30, total_samples=100, active_error_alert_count=0)
    assert risk.level == RISK_HIGH


def test_active_error_alert_forces_high_risk_even_with_good_availability():
    risk = estimate_downtime_risk(unavailable_samples=0, total_samples=100, active_error_alert_count=1)
    assert risk.level == RISK_HIGH


def test_anomalous_load_detected_on_large_deviation():
    assert is_anomalous_load(actual_value=100, baseline_value=40) is True


def test_normal_load_within_tolerance_not_anomalous():
    assert is_anomalous_load(actual_value=45, baseline_value=40) is False


def test_zero_baseline_never_anomalous():
    assert is_anomalous_load(actual_value=50, baseline_value=0) is False
