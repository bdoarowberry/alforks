CREATE TABLE demo.source_one (
    id int NOT NULL,
    amount decimal(18, 2) NULL
);
GO

CREATE TABLE demo.source_two (
    id int NOT NULL,
    amount decimal(18, 2) NULL
);
GO

CREATE TABLE demo.output (
    id int NOT NULL,
    result_value decimal(18, 2) NULL,
    other_value decimal(18, 2) NULL
);
GO

CREATE TABLE demo.union_out (
    id int NOT NULL,
    result_value decimal(18, 2) NULL
);
GO

CREATE TABLE demo.branch_out (
    result_value decimal(18, 2) NULL
);
GO

CREATE TABLE demo.cross_out (
    id int NOT NULL,
    left_value decimal(18, 2) NULL,
    right_value decimal(18, 2) NULL
);
GO
