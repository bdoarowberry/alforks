CREATE PROCEDURE demo.load_param @p int = NULL AS
BEGIN
    SET @p = ISNULL(@p, (SELECT MAX(o.v) FROM demo.other o));
    INSERT INTO demo.output (id, result_value) SELECT s.id, @p FROM demo.source s;
END
GO
CREATE PROCEDURE demo.caller AS
EXEC demo.load_param @p = 5;
GO
