CREATE PROCEDURE demo.load_output
    @batch_value int
AS
BEGIN
    INSERT INTO demo.output (id, result_value)
    SELECT s.id, @batch_value
    FROM demo.source AS s;
END;
GO

CREATE PROCEDURE demo.load_other
    @other_input int
AS
BEGIN
    UPDATE demo.output
    SET other_value = @other_input;
END;
GO
