CREATE TABLE dbo.unpivot_tgt (id int, attr_name varchar(20), attr_val int);
GO
CREATE PROCEDURE dbo.stage_wide AS
BEGIN
    INSERT INTO dbo.wide_src (id, col_x, col_y)
    SELECT f.id, f.x, f.y FROM dbo.raw_wide f;
END
GO
CREATE PROCEDURE dbo.load_unpivot2 AS
BEGIN
    INSERT INTO dbo.unpivot_tgt (id, attr_name, attr_val)
    SELECT id, attr_name, attr_val
    FROM dbo.wide_src
    UNPIVOT (attr_val FOR attr_name IN (col_x, col_y)) AS u;
END
GO
