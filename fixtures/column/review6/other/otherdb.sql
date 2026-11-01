USE OtherDb;
GO
CREATE TABLE dbo.Thing (id int, col_a int);
GO
CREATE FUNCTION dbo.FnOther (@k int) RETURNS TABLE AS RETURN (SELECT t.col_a FROM dbo.Thing AS t WHERE t.id = @k);
GO
