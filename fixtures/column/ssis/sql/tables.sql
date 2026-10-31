-- Invented tables loaded by the SSIS fixture packages (fixtures/column/ssis/*/).
USE demo_db;
GO
CREATE TABLE demo.stage (
    stage_id int IDENTITY(1, 1) NOT NULL,
    amount nvarchar(50) NULL,
    unmapped_column int NULL
);
GO
CREATE TABLE demo.orders (
    order_id int NOT NULL,
    amount decimal(18, 2) NULL,
    status_code char(1) NULL,
    region_code varchar(10) NULL,
    flag int NULL
);
CREATE TABLE demo.region (region_code varchar(10) NOT NULL, region_label varchar(50) NULL);
CREATE TABLE demo.split_target (order_id int NOT NULL, amount decimal(18, 2) NULL);
CREATE TABLE demo.derived_target (order_id int NOT NULL, chosen_amount decimal(18, 2) NULL);
CREATE TABLE demo.lookup_target (order_id int NOT NULL, region_label varchar(50) NULL);
CREATE TABLE demo.query_target (order_id int NOT NULL, doubled_amount decimal(18, 2) NULL);
CREATE TABLE demo.variable_target (order_id int NOT NULL, amount decimal(18, 2) NULL);
CREATE TABLE demo.copy_target (copy_value varchar(10) NULL);
CREATE TABLE demo.script_target (amount decimal(18, 2) NULL);
CREATE TABLE demo.name_target (amount decimal(18, 2) NULL, other_value decimal(18, 2) NULL);
CREATE TABLE demo.legacy_target (amount_text varchar(30) NULL);
CREATE TABLE demo.audit (row_total int NULL);
CREATE TABLE demo.orders_archive (amount decimal(18, 2) NULL, region_code varchar(10) NULL);
CREATE TABLE demo.union_target (region_code varchar(10) NULL, total_amount decimal(18, 2) NULL);
CREATE TABLE demo.file_target (code varchar(10) NULL, amount decimal(18, 2) NULL);
CREATE TABLE demo.file_status (code varchar(10) NULL, last_amount decimal(18, 2) NULL);
GO
