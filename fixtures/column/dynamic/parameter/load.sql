CREATE PROCEDURE demo.load_from
    @source_column sysname = N'input_a',
    @mode int = 0
AS
BEGIN
    DECLARE @sql nvarchar(max);
    SET @sql = N'INSERT INTO demo.output (id, result_value) SELECT id, ' + QUOTENAME(@source_column) + N' FROM demo.source';
    EXEC (@sql);
END;
GO

CREATE PROCEDURE demo.run_loads
AS
BEGIN
    DECLARE @which sysname = N'input_b';
    EXEC demo.load_from;
    EXEC demo.load_from @source_column = @which;
END;
GO

CREATE PROCEDURE demo.load_uncalled
    @source_column sysname = N'input_a'
AS
BEGIN
    DECLARE @sql nvarchar(max);
    SET @sql = N'INSERT INTO demo.output2 (id, result_value) SELECT id, ' + @source_column + N' FROM demo.source';
    EXEC (@sql);
END;
GO
CREATE TABLE demo.output2 (id int NOT NULL, result_value int NULL);
GO
