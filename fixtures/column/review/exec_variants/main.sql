CREATE PROCEDURE [demo].[load_ev] @a int, @b int = 3 AS
INSERT INTO demo.output (id, result_value, other_value) VALUES (1, @a, @b);
GO
CREATE PROCEDURE demo.caller_ev AS
BEGIN
    DECLARE @a int;
    SELECT @a = MAX(s.input_a) FROM demo.source s;
    EXECUTE [demo].[load_ev] @a;
    EXEC demo.load_ev @b = 9, @a = @a;
END
GO
