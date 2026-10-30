CREATE TABLE demo.source (
    id int NOT NULL,
    input_a int NULL,
    input_b int NULL,
    use_b bit NOT NULL
);
GO

CREATE TABLE demo.output (
    id int NOT NULL,
    result_value int NULL,
    alt_value int NULL
);
GO
