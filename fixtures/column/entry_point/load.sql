CREATE TABLE demo.source_a (id int, amount int);
CREATE TABLE demo.source_b (id int, amount int);
CREATE TABLE demo.source_c (id int, amount int);
CREATE TABLE demo.result (id int, amount int);
GO
CREATE PROCEDURE demo.load_base AS
INSERT INTO demo.result (id, amount) SELECT a.id, a.amount FROM demo.source_a AS a;
GO
CREATE PROCEDURE demo.apply_override AS
UPDATE r SET amount = b.amount FROM demo.result AS r JOIN demo.source_b AS b ON b.id = r.id;
GO
CREATE PROCEDURE demo.run_all AS
BEGIN
  BEGIN TRY
    EXEC demo.load_base;
    EXEC demo.apply_override;
  END TRY
  BEGIN CATCH
    THROW;
  END CATCH
END;
GO
CREATE PROCEDURE demo.run_conditional @flag bit AS
BEGIN
  EXEC demo.load_base;
  IF @flag = 1
    EXEC demo.apply_override;
END;
GO
