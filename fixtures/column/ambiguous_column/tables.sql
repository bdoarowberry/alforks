CREATE TABLE demo.left_side (
    id int NOT NULL,
    shared_value int NULL
);
GO

CREATE TABLE demo.right_side (
    id int NOT NULL,
    shared_value int NULL,
    right_only int NULL
);
GO

CREATE TABLE demo.source (
    id int NOT NULL,
    input_a int NULL
);
GO

CREATE TABLE demo.output (
    id int NOT NULL,
    result_value int NULL,
    other_value int NULL
);
GO

CREATE TABLE demo.alias_out (
    id int NOT NULL,
    result_value int NULL
);
GO
