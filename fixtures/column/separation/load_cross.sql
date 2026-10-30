CREATE PROCEDURE demo.load_cross
AS
BEGIN
    INSERT INTO demo.cross_out (id, left_value, right_value)
    SELECT x.id, x.amount, y.amount
    FROM db_one.demo.shared AS x
    INNER JOIN db_two.demo.shared AS y ON y.id = x.id;
END;
GO
