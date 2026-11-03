CREATE TABLE dbo.src (id int, amount decimal(18,2));
GO
CREATE TABLE dbo.src_eu (id int, amount decimal(18,2));
GO
CREATE TABLE dbo.tgt (id int, amount decimal(18,2));
GO
CREATE PROCEDURE dbo.load_isnull
AS
BEGIN
    DECLARE @t varchar(3);   -- NULL; ISNULL returns the type of @t, so the replacement is cut to 3 characters: 'src'
    DECLARE @sql nvarchar(4000);
    SET @sql = N'INSERT INTO dbo.tgt (id, amount) SELECT id, amount FROM dbo.' + ISNULL(@t, 'src_eu');
    EXEC (@sql);
END
GO
