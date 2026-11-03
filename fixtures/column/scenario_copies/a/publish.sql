-- Invented: two definitions of one entry procedure; only this one returns early.
CREATE TABLE dbo.c_saved (value_col int);
CREATE TABLE dbo.c_stage (value_col int);
GO
CREATE PROCEDURE dbo.c_build AS INSERT dbo.c_saved (value_col) SELECT value_col FROM dbo.c_stage;
GO
CREATE PROCEDURE dbo.c_publish @reuse bit AS
BEGIN
  IF @reuse = 1 RETURN;
  EXEC dbo.c_build;
END
GO
