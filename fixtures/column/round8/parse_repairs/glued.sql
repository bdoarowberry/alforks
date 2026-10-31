CREATE PROCEDURE dbo.p2 @id INT AS
BEGIN
DECLARE @cols NVARCHAR(MAX) = ''
SELECT @cols + = ',' + c.name FROM sys.columns c WHERE c.object_id = OBJECT_ID('dbo.t')
DECLARE @tv TABLE (a INT, b INT)
INSERT INTO @tv (x.a, x.b) SELECT a, b FROM dbo.src WHERE a IN (SELECT a FROM dbo.other) ORDER BY b
CREATE CLUSTERED INDEX ix ON #w (a)
WITH (PAD_INDEX = OFF, STATISTICS_NORECOMPUTE = OFF, SORT_IN_TEMPDB = OFF, DROP_EXISTING = OFF, ONLINE = OFF, ALLOW_ROW_LOCKS = ON, ALLOW_PAGE_LOCKS = ON) ON [PRIMARY]
UPDATE #w SET a = a * -1, b = b * -1 WHERE c <> 'X'
EXEC dbo.q @a = @id, @b = 1, @c = 0
DECLARE @d DATE = (SELECT MAX(d) AS m FROM dbo.cal)
DECLARE @n INT = (SELECT DATEPART(month, @d))
END
GO
CREATE TABLE dbo.t (a INT, b INT);
CREATE TABLE dbo.src (a INT, b INT);
CREATE PROCEDURE dbo.p4 AS
INSERT INTO demo..t (a, b) SELECT s.a, s.b FROM dbo.src s
GO
