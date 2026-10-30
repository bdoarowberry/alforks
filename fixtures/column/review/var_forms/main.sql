CREATE TABLE demo.source (id int, input_a int, input_b int);
GO
CREATE TABLE demo.output (id int, result_value int, other_value int);
GO
CREATE PROCEDURE demo.calc @x int AS RETURN @x * 2;
GO
CREATE PROCEDURE demo.load_vf AS
BEGIN
    DECLARE @a int = 0, @b int = 0, @c int = 0;
    SET @a += (SELECT MAX(s.input_a) FROM demo.source s);
    UPDATE demo.source SET @b = input_b WHERE id = 1;
    EXEC @c = demo.calc @x = 5;
    INSERT INTO demo.output (id, result_value, other_value) VALUES (@a, @b, @c);
END
GO
