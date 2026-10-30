CREATE TABLE demo.source (id int, input_a int, input_b int);
GO
CREATE TABLE demo.output (
    row_id int IDENTITY(1,1) NOT NULL,
    id int,
    result_value int,
    other_value int,
    total AS (result_value + other_value) PERSISTED,
    loaded_at datetime NOT NULL CONSTRAINT df_loaded DEFAULT (GETDATE())
);
GO
CREATE PROCEDURE demo.load_c AS
INSERT INTO demo.output SELECT s.id, s.input_a, s.input_b, GETDATE() FROM demo.source s;
GO
