CREATE TABLE dbo.wide_src (id int, col_x int, col_y int);
GO
CREATE TABLE dbo.unpivot_tgt (id int, attr_name varchar(20), attr_val int);
GO
CREATE PROCEDURE dbo.load_apply_values AS
BEGIN
    INSERT INTO dbo.unpivot_tgt (id, attr_name, attr_val)
    SELECT w.id, v.name, v.val
    FROM dbo.wide_src AS w
    CROSS APPLY (VALUES (w.col_x, 'x'), (w.col_y, 'y')) AS v(val, name);
END
GO
