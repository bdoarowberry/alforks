USE land_db;
GO

CREATE TABLE demo.raw_feed (
    id int NOT NULL,
    raw_amount int NULL
);
GO

-- Same schema.name and column as stage_db.demo.staged, in a different database.
CREATE TABLE demo.staged (
    id int NOT NULL,
    staged_amount int NULL
);
GO
