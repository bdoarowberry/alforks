CREATE TABLE demo.source (id int, input_a int);
GO
CREATE PROCEDURE demo.load_with_dead_maintenance AS
BEGIN
    DECLARE @index_sql nvarchar(max) = N'';
    /* SET @index_sql = N'INSERT INTO demo.output (id, result_value) SELECT 1, 2'; */
    IF LEN(@index_sql) > 0 EXEC (@index_sql);
    INSERT INTO demo.output (id, result_value) SELECT s.id, s.input_a FROM demo.source s;
END
GO
CREATE PROCEDURE demo.load_with_live_dynamic AS
BEGIN
    DECLARE @sql nvarchar(max) = N'';
    SET @sql = N'INSERT INTO demo.output2 (id, result_value) SELECT id, input_a FROM demo.source';
    EXEC (@sql);
    INSERT INTO demo.output2 (id, result_value) SELECT s.id, s.input_a FROM demo.source s;
END
GO
CREATE PROCEDURE demo.load_with_parameter_sql @sql nvarchar(max) = N'' AS
BEGIN
    EXEC sp_executesql @sql;
    INSERT INTO demo.output3 (id, result_value) SELECT s.id, s.input_a FROM demo.source s;
END
GO
