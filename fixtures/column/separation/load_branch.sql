CREATE PROCEDURE demo.load_branch
    @mode int
AS
BEGIN
    IF @mode = 1
    BEGIN
        INSERT INTO demo.branch_out (result_value)
        SELECT o.amount
        FROM demo.source_one AS o;
    END
    ELSE
    BEGIN
        INSERT INTO demo.branch_out (result_value)
        SELECT t.amount
        FROM demo.source_two AS t;
    END
END;
GO
