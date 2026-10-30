CREATE PROCEDURE demo.run_named
AS
BEGIN
    EXEC demo.load_output @batch_value = 42;
END;
GO

CREATE PROCEDURE demo.run_positional
AS
BEGIN
    EXEC demo.load_output 7;
END;
GO
