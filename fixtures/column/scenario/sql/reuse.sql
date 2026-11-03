-- Invented shape from a tester trial: with @reuse = 1 only the saved-result SELECT can run.
CREATE TABLE dbo.u_saved (value_col int);
CREATE TABLE dbo.u_stage (value_col int);
CREATE TABLE dbo.u_report (value_col int);
GO
CREATE PROCEDURE dbo.u_build_stage AS INSERT dbo.u_stage (value_col) SELECT 1;
GO
CREATE PROCEDURE dbo.u_build_result AS INSERT dbo.u_saved (value_col) SELECT value_col FROM dbo.u_stage;
GO
CREATE PROCEDURE dbo.u_load_report AS INSERT dbo.u_report (value_col) SELECT value_col FROM dbo.u_saved;
GO
CREATE PROCEDURE dbo.u_publish @reuse bit AS
BEGIN
  IF @reuse = 1
  BEGIN
    SELECT value_col FROM dbo.u_saved;
    RETURN;
  END

  SELECT value_col FROM dbo.u_stage;
  EXEC dbo.u_build_stage;
  EXEC dbo.u_build_result;
END
GO
