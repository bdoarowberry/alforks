CREATE TABLE demo.source_a (id int, amount int);
CREATE TABLE demo.source_b (id int, amount int);
CREATE TABLE demo.source_c (id int, amount int);
CREATE TABLE demo.result (id int, amount int);
GO
CREATE PROCEDURE demo.apply_override AS
UPDATE r SET amount = b.amount FROM demo.result AS r JOIN demo.source_b AS b ON b.id = r.id;
GO
CREATE PROCEDURE demo.apply_other AS
UPDATE r SET amount = c.amount FROM demo.result AS r JOIN demo.source_c AS c ON c.id = r.id;
GO
CREATE PROCEDURE demo.run_either @flag bit AS
BEGIN
  IF @flag = 1
    EXEC demo.apply_override;
  ELSE
    EXEC demo.apply_other;
END;
GO
