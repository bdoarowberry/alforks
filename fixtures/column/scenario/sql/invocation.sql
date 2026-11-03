-- Invented shape from a tester trial: the entry procedure only calls loaders, inside TRY/CATCH; the
-- saved table is also loaded by separate procedures; a later reassignment of @mode under a date test.
CREATE TABLE dbo.i_saved (value_col int);
CREATE TABLE dbo.i_regular (value_col int);
CREATE TABLE dbo.i_interim (value_col int);
CREATE TABLE dbo.i_log (msg varchar(50));
CREATE TABLE dbo.i_report (value_col int);
GO
CREATE PROCEDURE dbo.i_load_regular AS
    INSERT dbo.i_saved (value_col) SELECT value_col FROM dbo.i_regular;
GO
CREATE PROCEDURE dbo.i_load_interim AS
    INSERT dbo.i_saved (value_col) SELECT value_col FROM dbo.i_interim;
GO
-- Loaded on its own schedule, never called by the entry procedure.
CREATE PROCEDURE dbo.i_load_regular_stage AS
    INSERT dbo.i_regular (value_col) SELECT 1;
GO
CREATE PROCEDURE dbo.i_report_load AS
    INSERT dbo.i_report (value_col) SELECT value_col FROM dbo.i_saved;
GO
CREATE PROCEDURE dbo.i_publish @mode int, @refresh bit, @as_of date, @period_end date AS
BEGIN
  DECLARE @msg varchar(50);
  BEGIN TRY
    IF @mode = 1
    BEGIN
      IF @as_of = @period_end SET @mode = 0;
    END
    IF @refresh = 0
    BEGIN
      SELECT value_col FROM dbo.i_saved;
      RETURN;
    END
    SET @msg = 'rebuilt';
    IF @mode = 1
      EXEC dbo.i_load_interim;
    ELSE
      EXEC dbo.i_load_regular;
    INSERT dbo.i_log (msg) SELECT @msg;
  END TRY
  BEGIN CATCH
    THROW;
  END CATCH
END
GO
