CREATE PROCEDURE dbo.load1 AS
BEGIN
  SET NOCOUNT ON
  BEGIN TRY
    BEGIN TRANSACTION
    TRUNCATE TABLE dbo.dest1
    INSERT INTO dbo.dest1 WITH (TABLOCK)
      ( c01, c02, c03, c04, c05, c06, c07, c08, c09, c10, c11, c12, c13, c14, c15, c16, c17, c18, c19, c20, c21, c22, c23, c24, c25, c26, c27, c28, c29, c30, c31, c32, c33, c34, c35, c36, c37, c38, c39, c40, c41, c42, c43, c44, c45, c46, c47, c48, c49, c50, c51, c52, c53, c54, c55, c56, c57, c58, c59, c60, 
        [ref_no], -- the reference
        x )
    SELECT s.c01, s.c02, s.c03, s.c04, s.c05, s.c06, s.c07, s.c08, s.c09, s.c10, s.c11, s.c12, s.c13, s.c14, s.c15, s.c16, s.c17, s.c18, s.c19, s.c20, s.c21, s.c22, s.c23, s.c24, s.c25, s.c26, s.c27, s.c28, s.c29, s.c30, s.c31, s.c32, s.c33, s.c34, s.c35, s.c36, s.c37, s.c38, s.c39, s.c40, s.c41, s.c42, s.c43, s.c44, s.c45, s.c46, s.c47, s.c48, s.c49, s.c50, s.c51, s.c52, s.c53, s.c54, s.c55, s.c56, s.c57, s.c58, s.c59, s.c60, 
           LTRIM(RTRIM(s.source_ref)) AS ref_no, 1
    FROM stage.src AS s
    WHERE s.c01 IS NOT NULL
    COMMIT TRANSACTION
  END TRY
  BEGIN CATCH
    ROLLBACK
  END CATCH
END
GO
CREATE PROCEDURE dbo.load2 AS
;WITH q AS (SELECT source_ref FROM stage.src)
INSERT INTO demo..dest2 (ref_no) SELECT q.source_ref FROM q
GO
CREATE PROCEDURE dbo.load3 AS
IF OBJECT_ID('tempdb..#t') IS NOT NULL DROP TABLE #t
INSERT dbo.dest3 (ref_no)
SELECT DISTINCT s.source_ref FROM stage.src s
GO
CREATE PROCEDURE [dbo].[load4] AS
BEGIN
DECLARE @n int = 0
WHILE @n < 2
BEGIN
  INSERT INTO [dbo].[dest4] ([ref_no]) SELECT s.[source_ref] FROM [stage].[src] s
  SET @n += 1
END
END
GO
CREATE PROCEDURE dbo.load5 AS
INSERT INTO dbo.dest5 (ref_no)
SELECT s.source_ref FROM stage.src s
UNION
SELECT s.source_ref FROM stage.src s WHERE 1=0
GO
CREATE PROCEDURE dbo.load6 AS
BEGIN
  IF EXISTS (SELECT 1 FROM stage.src)
  BEGIN
    INSERT INTO dbo.dest6 (ref_no) SELECT s.source_ref FROM stage.src s
  END
  ELSE
    PRINT 'none'
END
GO
CREATE PROCEDURE dbo.load7 AS
SELECT 1
INSERT INTO dbo.dest7 (ref_no) SELECT s.source_ref FROM stage.src s OPTION (RECOMPILE)
GO
