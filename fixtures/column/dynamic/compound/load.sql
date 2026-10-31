CREATE PROCEDURE demo.load_compound
AS
BEGIN
    DECLARE @sql nvarchar(max) = N'';
    SET @sql += N'INSERT INTO demo.output (id, result_value) ';
    SET @sql += N'SELECT id, input_a ' + N'FROM demo.source';
    EXEC sp_executesql @sql;
END;
GO
CREATE TABLE demo.source (id int NOT NULL, input_a int NULL);
GO
CREATE TABLE demo.output (id int NOT NULL, result_value int NULL);
GO
