CREATE TABLE demo.source (id int, input_a int, input_b int, dt datetime, k int);
GO
CREATE TABLE demo.other (id int, v int, dt datetime, k int);
GO
CREATE TABLE demo.output (id int, result_value int, other_value int);
GO
CREATE TABLE demo.audit (id int, old_value int, new_value int);
GO

