CREATE TABLE stage.src (id int, code varchar(10), flag int, amount decimal(18,2), d date, date2 date);
GO
CREATE TABLE stage.lookup (code varchar(10), label varchar(50));
GO
CREATE TABLE dbo.out (id int, c_session varchar(50), c_cond varchar(50), c_threepart varchar(50),
    c_bracket varchar(50), c_schemabound varchar(50), c_shadow varchar(50), c_prefix varchar(50),
    c_decimal decimal(18,2), c_now datetime, c_nested varchar(50));
GO
