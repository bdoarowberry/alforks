CREATE PROCEDURE demo.load_pair
AS
BEGIN
    INSERT INTO demo.output (id, result_value, other_value)
    SELECT a.id, a.amount, b.amount
    FROM demo.source_one AS a
    INNER JOIN demo.source_two AS b ON b.id = a.id;
END;
GO
