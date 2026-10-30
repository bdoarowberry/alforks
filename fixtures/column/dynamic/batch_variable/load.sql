CREATE TABLE demo.source (id int NOT NULL, input_a int NULL, input_b int NULL);
GO
CREATE TABLE demo.source_b (id int NOT NULL, input_b int NULL);
GO
CREATE TABLE demo.output (id int NOT NULL, result_value int NULL);
GO
CREATE TABLE demo.output2 (id int NOT NULL, result_value int NULL);
GO

CREATE PROCEDURE demo.load_with_batch_variable
    @offset int = 1
AS
BEGIN
    DECLARE @sql nvarchar(max) = N'INSERT INTO demo.output (id, result_value) SELECT id, input_a + @delta FROM demo.source';
    EXEC sp_executesql @sql, N'@delta int', @delta = @offset;
END;
GO

CREATE PROCEDURE demo.load_truncated
AS
BEGIN
    -- varchar(6) keeps only 'source': the executed SQL reads demo.source, not demo.source_b.
    DECLARE @table varchar(6) = 'source_b';
    DECLARE @sql nvarchar(max) = N'INSERT INTO demo.output2 (id, result_value) SELECT id, input_b FROM demo.' + @table;
    EXEC (@sql);
END;
GO
