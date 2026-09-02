"""Политика хранения имени документа (print_jobs.document_name).

document_name из события 307 — это то, что пользователь напечатал, и оно
может быть чувствительным (например, "Зарплата_Иванов_ноябрь.xlsx" или
"Договор_с_клиентом_X.pdf"). config.yaml -> document_name_policy управляет,
что реально попадает в БД:
  - "full"   — хранить как есть (поведение MVP по умолчанию, для обратной совместимости);
  - "masked" — хранить только расширение файла, само имя скрыто;
  - "none"   — не хранить вообще (NULL).

Уже вставленные ранее document_name эта настройка не затрагивает — влияет
только на новые задания, начиная с момента, когда настройку поменяли.
"""
from typing import Optional

MASK_PLACEHOLDER = "•••"


def apply_document_name_policy(document_name: Optional[str], policy: str) -> Optional[str]:
    if policy == "none":
        return None
    if policy == "masked":
        if not document_name:
            return document_name
        if "." in document_name:
            ext = document_name.rsplit(".", 1)[-1].strip()
            if ext and len(ext) <= 10 and ext.isalnum():
                return f"{MASK_PLACEHOLDER}.{ext}"
        return MASK_PLACEHOLDER
    return document_name  # "full" (или неизвестное значение — безопасный дефолт как раньше)
