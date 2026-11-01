CREATE TABLE dbo.Target (id int, col_a int, col_b int);
CREATE TABLE dbo.NewValues (id int, cap int, col_a int, col_b int);
CREATE TABLE dbo.Capped (id int, col_a int, col_b int);
GO
CREATE PROCEDURE dbo.load_target AS
BEGIN
    INSERT INTO dbo.Target (id, col_a, col_b)
    SELECT id, col_a, col_b FROM dbo.NewValues;

    UPDATE t
    SET col_a = CASE WHEN x.total_a <= s.cap THEN t.col_a ELSE s.cap END,
        col_b = CASE WHEN x.total_a <= s.cap THEN t.col_b ELSE s.cap END
    FROM dbo.Target AS t
    JOIN dbo.NewValues AS s ON s.id = t.id
    CROSS APPLY (SELECT COALESCE(t.col_a, 0) + COALESCE(t.col_b, 0) AS total_a) AS x;

    INSERT INTO dbo.Capped (id, col_a, col_b)
    SELECT id, col_a, col_b FROM dbo.Target;
END
