-- Invented: one value route reaches the requested boundary (db_a), one ends at a staging table no code loads.
USE db_a;
GO
CREATE TABLE dbo.loan (loan_id int, customer_id int, amount decimal(18,2));
GO
USE db_b;
GO
CREATE TABLE dbo.stage (loan_id int, customer_id int, region varchar(10));
CREATE TABLE dbo.report (loan_id int, customer_id int);
GO
CREATE PROCEDURE dbo.load_report AS
    INSERT INTO dbo.report (loan_id, customer_id)
    SELECT s.loan_id, CASE WHEN s.region = 'EU' THEN l.customer_id ELSE COALESCE(l.customer_id, s.customer_id) END
    FROM dbo.stage s LEFT JOIN db_a.dbo.loan l ON l.loan_id = s.loan_id;
GO
-- A second writer reads the same staging column through an unqualified name (the schema is inferred).
CREATE PROCEDURE dbo.load_report_ids AS
    INSERT INTO dbo.report (loan_id) SELECT loan_id FROM stage;
GO
