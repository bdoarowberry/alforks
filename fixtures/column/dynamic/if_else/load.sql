CREATE PROCEDURE demo.load_branch
    @mode int
AS
BEGIN
    DECLARE @sql nvarchar(max);
    DECLARE @target sysname;
    DECLARE @source sysname;
    DECLARE @column sysname;

    IF @mode = 1
    BEGIN
        SET @target = N'demo.output_a';
        SET @source = N'demo.source_a';
        SET @column = N'input_a';
    END
    ELSE
    BEGIN
        SET @target = N'demo.output_b';
        SET @source = N'demo.source_b';
        SET @column = N'input_b';
    END;

    SET @sql = N'INSERT INTO ' + @target + N' (id, result_value) SELECT id, ' + @column + N' FROM ' + @source;
    EXEC (@sql);
END;
GO
