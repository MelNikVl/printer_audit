"""Импортирует/обновляет сопоставление пользователь -> отдел из CSV-файла
(config/users_departments.csv, путь настраивается в config.yaml).

Формат CSV (заголовок обязателен):
    user_name,department_name,cost_center_code

Запуск после каждого изменения CSV:

    python scripts\\sync_users_departments.py

Скрипт не деактивирует и не удаляет пользователей, отсутствующих в CSV —
только создаёт новых и обновляет department_id у существующих. После обновления
пересчитывает department_id у уже накопленных print_jobs для этих пользователей
(чтобы отчёты по отделам не "теряли" задания, напечатанные до правки CSV).
"""
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from printaudit.config import get_settings  # noqa: E402
from printaudit.database import SessionLocal  # noqa: E402
from printaudit.models import Department, User  # noqa: E402


def main() -> None:
    settings = get_settings()
    csv_path = settings.users_departments_csv
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV не найден: {csv_path}")

    session = SessionLocal()
    try:
        dept_cache: dict[str, Department] = {}
        created_depts = created_users = updated_users = 0

        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                user_name = (row.get("user_name") or "").strip()
                dept_name = (row.get("department_name") or "").strip()
                cost_center = (row.get("cost_center_code") or "").strip() or None
                if not user_name:
                    continue

                department_id = None
                if dept_name:
                    dept = dept_cache.get(dept_name)
                    if dept is None:
                        dept = session.query(Department).filter_by(name=dept_name).first()
                        if dept is None:
                            dept = Department(name=dept_name, cost_center_code=cost_center)
                            session.add(dept)
                            session.flush()
                            created_depts += 1
                        dept_cache[dept_name] = dept
                    department_id = dept.id

                user = session.get(User, user_name)
                if user is None:
                    session.add(User(user_name=user_name, department_id=department_id, is_active=True))
                    created_users += 1
                else:
                    user.department_id = department_id
                    updated_users += 1

        session.commit()

        # Пересчитать department_id у уже записанных print_jobs по актуальной таблице users.
        session.execute(
            text(
                """
                UPDATE print_jobs
                SET department_id = (
                    SELECT department_id FROM users WHERE users.user_name = print_jobs.user_name
                )
                """
            )
        )
        session.commit()

        print(
            f"Отделы создано: {created_depts}. Пользователи: создано {created_users}, "
            f"обновлено {updated_users}."
        )
    finally:
        session.close()


if __name__ == "__main__":
    main()
