CREATE TABLE demo.source (
    id int NOT NULL,
    input_a int NULL
);
GO

CREATE TABLE demo.decoy (
    id int NOT NULL,
    input_b int NULL
);
GO

CREATE TABLE demo.output (
    id int NOT NULL,
    result_value int NULL
);
GO

CREATE TABLE demo.audit_log (
    message nvarchar(400) NULL
);
GO
