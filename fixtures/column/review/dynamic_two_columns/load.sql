CREATE TABLE demo.work (id int, part_a int, part_b int);
GO
CREATE PROCEDURE demo.fill_work AS
BEGIN
    DECLARE @sql nvarchar(max) = N'INSERT INTO demo.work (id, part_a, part_b) SELECT id, a, b FROM ' + dbo.pick_source();
    EXEC sp_executesql @sql;
END
GO
CREATE PROCEDURE demo.load_output AS
INSERT INTO demo.output (id, result_value) SELECT w.id, w.part_a + w.part_b FROM demo.work w;
GO
