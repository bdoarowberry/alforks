CREATE TABLE demo.source (
    id int NOT NULL,
    input_a int NULL,
    input_b int NULL
);
GO

CREATE TABLE demo.output (
    row_id int IDENTITY(1, 1) NOT NULL,
    first_value int NULL,
    result_value int NULL
);
GO
