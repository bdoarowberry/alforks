-- Invented shapes from a tester trial: EXEC (@sql) without a semicolon, a comment, then a SET.
CREATE TABLE dbo.audit_log (stamp datetime, source_id int);
CREATE TABLE dbo.src_list (id int);
CREATE TABLE dbo.out_stamp (stamp datetime);
CREATE TABLE dbo.target_table (id int);
CREATE TABLE dbo.out_count (n int);
GO
CREATE PROCEDURE dbo.capture @source_table sysname AS
BEGIN
    DECLARE @sql nvarchar(max), @last_stamp datetime;
    CREATE TABLE #last_load (stamp datetime);
    SELECT @sql = N'INSERT INTO #last_load (stamp) '
                + N'SELECT MAX(stamp) FROM dbo.audit_log '
                + N'WHERE source_id IN (SELECT id FROM ' + @source_table + N')';
    EXEC (@sql)
    -- Capture the result from the temporary table.
    SET @last_stamp = (SELECT stamp FROM #last_load);
    INSERT INTO dbo.out_stamp (stamp) SELECT @last_stamp;
END
GO
CREATE PROCEDURE dbo.run_capture AS EXEC dbo.capture @source_table = N'dbo.src_list';
GO
-- The SQL comes from a function: not evaluated, for that reason (not "runs elsewhere").
CREATE PROCEDURE dbo.reindex AS
BEGIN
    DECLARE @index_sql nvarchar(max), @row_count int;
    SET @index_sql = dbo.build_index_script(N'target_table');
    IF LEN(@index_sql) > 0 EXEC (@index_sql)
    -- Count rows after index maintenance.
    SET @row_count = (SELECT COUNT(*) FROM dbo.target_table);
    INSERT INTO dbo.out_count (n) SELECT @row_count;
END
GO
-- A multi-line EXEC call keeps its argument lines.
CREATE PROCEDURE dbo.caller AS
    EXEC dbo.capture
        @source_table = N'dbo.src_list'
GO
