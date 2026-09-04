"""/printers — список физических устройств (не очередей, см.
printaudit.models.PrinterDevice) с техническим состоянием, расходниками,
ошибками и прогнозом, и /printers/{id} — карточка устройства. Доступно
любому вошедшему пользователю (require_login), тот же уровень доступа, что
и у /print-jobs, /by-printer — это отчётный/мониторинговый раздел, не
административный (сами устройства заводятся/связываются в /admin, см.
printaudit.monitoring.devices)."""
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from printaudit.forecasting import LOAD_METRICS, METRIC_DOWNTIME_RISK, METRIC_TONER_EXHAUSTION
from printaudit.models import AppUser, PrintServer, Site
from printaudit.monitoring.device_queries import get_device_detail, list_devices
from webapp.deps import csrf_token, get_db, require_login
from webapp.templating import templates

router = APIRouter()


def _group_forecasts(forecasts) -> dict:
    """ForecastRun.forecast_json -> структура, удобная для шаблона: прогноз
    нагрузки по метрике/горизонту, отдельно прогноз(ы) исчерпания
    расходников (один на supply_type) и риск простоя (см.
    printaudit.forecasting.pipeline за формой каждого JSON)."""
    load = {metric: {} for metric in LOAD_METRICS}
    toner = []
    downtime_risk = None
    for row in forecasts:
        payload = json.loads(row.forecast_json) if row.forecast_json else {"insufficient_history": True}
        if row.metric in LOAD_METRICS:
            load[row.metric][row.horizon_days] = payload
        elif row.metric.startswith(f"{METRIC_TONER_EXHAUSTION}:"):
            toner.append(payload)
        elif row.metric == METRIC_DOWNTIME_RISK:
            downtime_risk = payload
    return {"load": load, "toner": toner, "downtime_risk": downtime_risk}


@router.get("/printers")
def printers_page(
    request: Request,
    site_id: Optional[int] = None,
    print_server_id: Optional[int] = None,
    status: Optional[str] = None,
    model: Optional[str] = None,
    monitoring_source: Optional[str] = None,
    has_active_errors: Optional[bool] = None,
    low_supply_only: Optional[bool] = None,
    no_data_only: Optional[bool] = None,
    q: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: AppUser = Depends(require_login),
):
    rows = list_devices(
        db, site_id=site_id, print_server_id=print_server_id, status=status or None, model=model or None,
        monitoring_source=monitoring_source or None, has_active_errors=has_active_errors,
        low_supply_only=low_supply_only, no_data_only=no_data_only, q=q or None,
    )
    from printaudit.models import PrinterDevice

    models = sorted({d[0] for d in db.query(PrinterDevice.model).filter(PrinterDevice.model.isnot(None)).distinct().all()})

    return templates.TemplateResponse(
        "printers.html",
        {
            "request": request, "current_user": current_user, "csrf_token": csrf_token(request),
            "rows": rows, "site_id": site_id, "print_server_id": print_server_id, "status": status or "",
            "model": model or "", "monitoring_source": monitoring_source or "", "has_active_errors": bool(has_active_errors),
            "low_supply_only": bool(low_supply_only), "no_data_only": bool(no_data_only), "q": q or "",
            "sites": db.query(Site).order_by(Site.name).all(),
            "print_servers": db.query(PrintServer).order_by(PrintServer.server_name).all(),
            "models": models,
        },
    )


@router.get("/printers/{device_id}")
def printer_detail_page(
    device_id: int, request: Request, db: Session = Depends(get_db), current_user: AppUser = Depends(require_login),
):
    detail = get_device_detail(db, device_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Устройство не найдено")
    forecasts = _group_forecasts(detail.forecasts)
    counter_points = [
        {"collected_at": s.collected_at.isoformat(), "total_pages": s.total_pages}
        for s in detail.counter_history_points if s.total_pages is not None
    ]
    return templates.TemplateResponse(
        "printer_detail.html",
        {
            "request": request, "current_user": current_user, "csrf_token": csrf_token(request),
            "detail": detail, "forecasts": forecasts, "counter_points_json": json.dumps(counter_points),
        },
    )
