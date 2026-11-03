CREATE TABLE dbo.f_src (region varchar(10), city varchar(20), amt int, idx int, a varchar(10), b varchar(10), s varchar(20));
GO
CREATE TABLE dbo.f_tgt (region varchar(10), city varchar(20), total int, agg_txt varchar(400), cws varchar(50),
                        iif_v varchar(10), choose_v varchar(10), tc int, tcv int, pd date, gid int);
GO
CREATE PROCEDURE dbo.load_rollup AS
BEGIN
    INSERT INTO dbo.f_tgt (region, city, total, gid)
    SELECT region, city, SUM(amt), GROUPING_ID(region, city)
    FROM dbo.f_src
    GROUP BY ROLLUP (region, city);
END
GO
CREATE PROCEDURE dbo.load_gsets AS
BEGIN
    INSERT INTO dbo.f_tgt (region, city, total)
    SELECT region, city, SUM(amt)
    FROM dbo.f_src
    GROUP BY GROUPING SETS ((region), (city), ());
END
GO
CREATE PROCEDURE dbo.load_cube AS
BEGIN
    INSERT INTO dbo.f_tgt (region, city, total)
    SELECT ISNULL(region, 'ALL'), city, SUM(amt)
    FROM dbo.f_src
    GROUP BY CUBE (region, city);
END
GO
CREATE PROCEDURE dbo.load_funcs AS
BEGIN
    INSERT INTO dbo.f_tgt (region, agg_txt, cws, iif_v, choose_v, tc, tcv)
    SELECT region,
           STRING_AGG(city, ',') WITHIN GROUP (ORDER BY city),
           CONCAT_WS('-', MAX(a), MAX(b)),
           IIF(MAX(idx) > 1, MAX(a), 'none'),
           CHOOSE(MAX(idx), 'low', 'mid', 'high'),
           TRY_CAST(MAX(s) AS int),
           TRY_CONVERT(int, MAX(s))
    FROM dbo.f_src
    GROUP BY region;
END
GO
