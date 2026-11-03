-- An older copy without the early return.
CREATE PROCEDURE dbo.c_publish @reuse bit AS
BEGIN
  EXEC dbo.c_build;
END
GO
