-- Invented shape from a tester trial: indented calls with trailing comments inside TRY (no semicolons).
CREATE TABLE dbo.t_result (v int);
CREATE TABLE dbo.t_regular (v int);
CREATE TABLE dbo.t_interim (v int);
CREATE TABLE dbo.t_goto_out (v int);
GO
CREATE PROCEDURE dbo.t_step_a @m int AS SELECT 1;
GO
-- A GOTO: the scenario cannot be applied here.
CREATE PROCEDURE dbo.t_goto @mode int AS
BEGIN
  IF @mode = 1 GOTO done;
  INSERT dbo.t_goto_out(v) SELECT v FROM dbo.t_regular;
done:
  RETURN;
END
GO
CREATE PROCEDURE dbo.t_publish @mode int, @rebuild bit AS
BEGIN
  IF @rebuild = 1
  BEGIN TRY
    IF @mode = 0 INSERT dbo.t_result(v) SELECT v FROM dbo.t_regular;
    ELSE INSERT dbo.t_result(v) SELECT v FROM dbo.t_interim;
    EXEC dbo.t_step_a @mode -- first source
    EXEC dbo.t_step_b @mode -- second source
    EXEC dbo.t_step_c @mode -- third source
  END TRY
  BEGIN CATCH
    THROW;
  END CATCH
END
GO
-- Alignment shapes: a bracketed [End] inside CASE, a WHILE the index records as its own statement, and a
-- RETURN the index merges with the INSERT after it on one line.
CREATE TABLE dbo.t_shapes (v int, [End] int);
CREATE TABLE dbo.t_merged (v int);
GO
CREATE PROCEDURE dbo.t_align @p int AS
BEGIN
    DECLARE @n int = 0
    WHILE @n < 2
    BEGIN
        UPDATE s SET s.v = CASE WHEN s.[End] > 0 THEN 1 ELSE 2 END FROM dbo.t_shapes s
        SET @n += 1
    END
    IF @p = 1
        INSERT dbo.t_shapes (v) SELECT v FROM dbo.t_regular;
    ELSE
        INSERT dbo.t_shapes (v) SELECT v FROM dbo.t_interim;
END
GO
CREATE PROCEDURE dbo.t_merge @p int AS
BEGIN
    IF @p = 1 RETURN INSERT dbo.t_merged (v) SELECT v FROM dbo.t_regular;
END
GO
