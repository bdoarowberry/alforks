CREATE PROCEDURE dbo.p @Start date, @End date, @Update bit, @Set int AS
BEGIN
DELETE FROM dbo.t WHERE d BETWEEN @Start AND @End
SET @Set = 1
SELECT @Start AS s, @Update
EXEC dbo.q @Start, @End
INSERT INTO dbo.t (d) SELECT x.d FROM dbo.s x WHERE x.d BETWEEN @Start AND @End
END
