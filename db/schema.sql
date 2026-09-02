-- Справочная схема БД print_audit (диалект PostgreSQL для наглядности типов).
-- В реальном развёртывании таблицы создаются автоматически из
-- printaudit/models.py командой `python scripts\init_db.py` — этот файл
-- служит документацией и вариантом для ручного создания БД PostgreSQL,
-- если её настраивают отдельно от приложения.
--
-- Для SQLite синтаксис немного отличается (нет SERIAL/BOOLEAN как отдельных
-- типов, TIMESTAMPTZ -> TEXT/DATETIME) — но результат идентичен тому, что
-- создаёт SQLAlchemy, поэтому руками эту схему для SQLite создавать не нужно.

CREATE TABLE departments (
    id                 SERIAL PRIMARY KEY,
    name               VARCHAR(200) NOT NULL UNIQUE,
    cost_center_code   VARCHAR(50)
);

CREATE TABLE users (
    user_name      VARCHAR(200) PRIMARY KEY,   -- формат DOMAIN\username, как в событии 307
    department_id  INTEGER REFERENCES departments(id),
    is_active      BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE price_list (
    id                     SERIAL PRIMARY KEY,
    printer_name_pattern   VARCHAR(200) NOT NULL,   -- glob-паттерн, напр. 'HP-Color-3F*' или '*'
    is_color               BOOLEAN NOT NULL DEFAULT FALSE,
    price_per_page         REAL NOT NULL,
    currency               VARCHAR(10) NOT NULL DEFAULT 'KZT',
    priority               INTEGER NOT NULL DEFAULT 0  -- выше = проверяется раньше
);

CREATE TABLE print_jobs (
    id               SERIAL PRIMARY KEY,
    site_code        VARCHAR(50) NOT NULL,      -- код площадки, для будущей консолидации 4 объектов
    record_id        INTEGER NOT NULL,          -- EventRecordID из PrintService/Operational
    job_id           VARCHAR(50),               -- Job Id из тела события (не глобально уникален)
    time_created     TIMESTAMPTZ NOT NULL,
    user_name        VARCHAR(200) NOT NULL,
    document_name    VARCHAR(500),
    printer_name     VARCHAR(200) NOT NULL,
    total_pages      INTEGER NOT NULL DEFAULT 0,
    is_color         BOOLEAN,                   -- см. price_list — по имени очереди, не по заданию
    department_id    INTEGER REFERENCES departments(id),
    price_per_page   REAL,
    cost             REAL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_print_jobs_site_record UNIQUE (site_code, record_id)
);

CREATE INDEX idx_print_jobs_time_created ON print_jobs (time_created);
CREATE INDEX idx_print_jobs_user_name    ON print_jobs (user_name);
CREATE INDEX idx_print_jobs_department   ON print_jobs (department_id);
CREATE INDEX idx_print_jobs_printer      ON print_jobs (printer_name);
CREATE INDEX idx_print_jobs_site         ON print_jobs (site_code);

-- Курсор инкрементального чтения журнала событий, одна строка на площадку.
CREATE TABLE collector_state (
    site_code       VARCHAR(50) PRIMARY KEY,
    last_record_id  INTEGER NOT NULL DEFAULT 0,
    last_run_at     TIMESTAMPTZ
);
