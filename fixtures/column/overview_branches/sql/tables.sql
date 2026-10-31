-- Invented multi-branch fixture for the overview page (tests/test_column_overview.py).
-- core_db: the main sources and a manual adjustment table; partner_db: a staging table loaded by a
-- partner (no writer in these files); rpt_db: a file staging table (loaded by a package) and the report.
USE core_db;
GO
CREATE TABLE crm.customer (
    customer_id int NOT NULL,
    credit_limit decimal(18, 2) NULL,
    is_closed bit NOT NULL,
    is_active bit NOT NULL
);
CREATE TABLE crm.account (
    customer_id int NOT NULL,
    default_limit decimal(18, 2) NULL
);
CREATE TABLE crm.limit_adjustment (
    customer_id int NOT NULL,
    new_limit decimal(18, 2) NULL,
    approved bit NOT NULL
);
GO
USE partner_db;
GO
CREATE TABLE stage.partner_extract (
    customer_id int NOT NULL,
    credit_limit decimal(18, 2) NULL,
    batch_ok bit NOT NULL
);
GO
USE rpt_db;
GO
CREATE TABLE stage.file_limit (
    customer_id int NULL,
    limit_text nvarchar(50) NULL
);
CREATE TABLE report.customer_limit (
    customer_id int NOT NULL,
    credit_limit decimal(18, 2) NULL,
    source_code varchar(10) NULL
);
GO
