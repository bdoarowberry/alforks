CREATE PROCEDURE demo.load_ambiguous
AS
BEGIN
    -- shared_value exists in both joined tables; right_only exists only in right_side.
    INSERT INTO demo.output (id, result_value, other_value)
    SELECT l.id, shared_value, right_only
    FROM demo.left_side AS l
    INNER JOIN demo.right_side AS r ON r.id = l.id;

    -- x is not an alias of any table in this statement.
    INSERT INTO demo.alias_out (id, result_value)
    SELECT s.id, x.input_a
    FROM demo.source AS s;
END;
GO
