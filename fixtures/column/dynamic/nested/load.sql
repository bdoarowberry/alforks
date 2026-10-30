CREATE PROCEDURE demo.load_nested
AS
BEGIN
    DECLARE @outer nvarchar(max);
    SET @outer = N'DECLARE @inner nvarchar(max) = N''INSERT INTO demo.output (id, result_value) SELECT id, input_a FROM demo.source'';
EXEC (@inner);
EXEC (N''UPDATE demo.output SET result_value = input_b FROM demo.output JOIN demo.source ON source.id = output.id'');';
    EXEC (@outer);
END;
GO
