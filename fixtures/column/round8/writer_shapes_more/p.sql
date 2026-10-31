CREATE PROCEDURE dbo.l1 AS INSERT INTO dbo.dest1 (ref_no) SELECT s.source_ref FROM stage.src s
GO
CREATE OR ALTER PROCEDURE dbo.l2
AS
INSERT INTO dbo.dest2 (a, ref_no)
SELECT a = s.a, ref_no = s.source_ref FROM stage.src s
GO
ALTER PROCEDURE dbo.l3 (@p int = 1) AS
BEGIN
INSERT INTO dbo.dest3 (ref_no)
SELECT TOP 100 PERCENT s.source_ref
FROM stage.src s WITH (NOLOCK)
ORDER BY 1
END
GO
CREATE PROCEDURE dbo.l4 AS
BEGIN
INSERT INTO dbo.dest4
(
 a
,ref_no
)
SELECT
 s.a
,CASE WHEN s.a = 1 THEN s.source_ref
      ELSE RIGHT(s.source_ref, 4)
 END
FROM stage.src s
END
GO
CREATE PROCEDURE dbo.l5 AS
BEGIN
DELETE FROM dbo.dest5 WHERE a = 1;
INSERT INTO dbo.dest5 (ref_no) SELECT CAST(s.source_ref AS varchar(20)) FROM stage.src s
LEFT JOIN dbo.dest5 d ON d.ref_no = s.source_ref WHERE d.ref_no IS NULL
END
GO
CREATE PROCEDURE dbo.l6 AS
BEGIN
INSERT INTO dbo.dest6 (ref_no) SELECT x.source_ref FROM (SELECT source_ref, ROW_NUMBER() OVER (PARTITION BY source_ref ORDER BY a) rn FROM stage.src) x WHERE x.rn = 1
END
GO
