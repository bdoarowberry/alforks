CREATE TABLE demo.source (
    id int NOT NULL,
    input_a int NULL,
    input_text varchar(20) NULL
);
GO

CREATE TABLE demo.output (
    id int NOT NULL,
    result_value int NULL,
    other_value int NULL,
    parsed_value int NULL
);
GO
