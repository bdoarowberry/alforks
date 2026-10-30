CREATE TABLE demo.source (
    id int NOT NULL,
    input_a int NULL,
    is_active bit NOT NULL
);
GO

CREATE TABLE demo.lookup_raw (
    id int NOT NULL,
    region_code varchar(10) NULL
);
GO

CREATE TABLE demo.lookup (
    id int NOT NULL,
    region_code varchar(10) NULL
);
GO

CREATE TABLE demo.output (
    id int NOT NULL,
    result_value int NULL
);
GO
