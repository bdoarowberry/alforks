CREATE TABLE demo.output (
    id int NOT NULL,
    result_value int NULL
);
GO

CREATE PROCEDURE demo.load_output
AS
BEGIN
    INSERT INTO demo.output (id, result_value)
    SELECT s.id, s.staged_amount
    FROM stage_db.demo.staged AS s;
END;
GO
