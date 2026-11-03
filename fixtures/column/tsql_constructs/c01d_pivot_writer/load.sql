CREATE TABLE dbo.pivot_tgt (region varchar(10), a_amt int, b_amt int);
GO
CREATE PROCEDURE dbo.stage_sales AS
BEGIN
    INSERT INTO dbo.sales_src (region, cat, amt)
    SELECT r.region_code, r.category, r.amount FROM dbo.raw_feed r;
END
GO
CREATE PROCEDURE dbo.load_pivot AS
BEGIN
    INSERT INTO dbo.pivot_tgt (region, a_amt, b_amt)
    SELECT region, [A], [B]
    FROM dbo.sales_src
    PIVOT (SUM(amt) FOR cat IN ([A], [B])) AS p;
END
GO
