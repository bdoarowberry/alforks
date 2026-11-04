-- Invented shapes from a tester trial: a long concatenation (was "call chain deeper than 8 procedures")
-- whose only unknown is a date inside a quoted literal of the WHERE clause.
CREATE TABLE dbo.events_a (d datetime, v int);
CREATE TABLE dbo.events_b (d datetime, v int, x varchar(20));
CREATE TABLE dbo.out_latest (d datetime);
CREATE TABLE dbo.out_select (d varchar(30));
CREATE TABLE dbo.out_name (v int);
CREATE TABLE dbo.out_table (v int);
GO
CREATE PROCEDURE dbo.latest @source_table sysname, @as_of datetime AS
BEGIN
    DECLARE @sql nvarchar(max), @latest datetime;
    CREATE TABLE #result (d datetime);
    SET @sql = N'INSERT INTO #result (d) '
             + N'SELECT MAX(d) '
             + N'FROM '
             + @source_table
             + N' '
             + N'WHERE 1 = 1 '
             + N'AND v > 0 '
             + N'AND d <= '''
             + CONVERT(varchar(30), @as_of, 21)
             + N'''';
    EXEC (@sql)
    SET @latest = (SELECT MAX(d) FROM #result);
    INSERT INTO dbo.out_latest (d) SELECT @latest;
END
GO
CREATE PROCEDURE dbo.select_date @source_table sysname, @as_of datetime AS
BEGIN
    DECLARE @sql nvarchar(max);
    -- The unknown date would be the value written: not evaluated.
    SET @sql = N'INSERT INTO dbo.out_select (d) SELECT ''' + CONVERT(varchar(30), @as_of, 21) + N''' FROM ' + @source_table;
    EXEC (@sql)
END
GO
CREATE PROCEDURE dbo.filter_label @source_table sysname, @label varchar(20) AS
BEGIN
    DECLARE @sql nvarchar(max);
    -- An unknown string could hold a quote: not evaluated.
    SET @sql = N'INSERT INTO dbo.out_name (v) SELECT v FROM ' + @source_table + N' WHERE x = ''' + @label + N'''';
    EXEC (@sql)
END
GO
CREATE PROCEDURE dbo.date_table @as_of datetime AS
BEGIN
    DECLARE @sql nvarchar(max);
    -- A date outside quotes would be code: not evaluated.
    SET @sql = N'INSERT INTO dbo.out_table (v) SELECT v FROM t_' + CONVERT(varchar(8), @as_of, 112);
    EXEC (@sql)
END
GO
CREATE PROCEDURE dbo.run_latest @as_of datetime, @label varchar(20) AS
BEGIN
    EXEC dbo.latest @source_table = N'dbo.events_a', @as_of = @as_of;
    EXEC dbo.select_date @source_table = N'dbo.events_b', @as_of = @as_of;
    EXEC dbo.filter_label @source_table = N'dbo.events_b', @label = @label;
END
GO
