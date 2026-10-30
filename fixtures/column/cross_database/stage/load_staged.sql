USE stage_db;
GO

CREATE TABLE demo.staged (
    id int NOT NULL,
    staged_amount int NULL
);
GO

CREATE PROCEDURE demo.load_staged
AS
BEGIN
    INSERT INTO demo.staged (id, staged_amount)
    SELECT r.id, r.raw_amount
    FROM land_db.demo.raw_feed AS r;
END;
GO
