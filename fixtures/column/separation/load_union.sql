CREATE PROCEDURE demo.load_union
AS
BEGIN
    INSERT INTO demo.union_out (id, result_value)
    SELECT o.id, o.amount
    FROM demo.source_one AS o
    UNION ALL
    SELECT t.id, t.amount
    FROM demo.source_two AS t;
END;
GO
