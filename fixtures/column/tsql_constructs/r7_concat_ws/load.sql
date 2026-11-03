CREATE TABLE dbo.src (id int, amount decimal(18,2));
GO
CREATE TABLE dbo.src_eu (id int, amount decimal(18,2));
GO
CREATE TABLE dbo.tgt (id int, amount decimal(18,2));
GO
CREATE PROCEDURE dbo.load_ws
AS
BEGIN
    DECLARE @sql nvarchar(4000);
    -- At run time this is: INSERT INTO dbo.tgt (id, amount) SELECT id, amount FROM dbo.src _eu   (alias _eu on dbo.src)
    SET @sql = CONCAT_WS(N' ', N'INSERT INTO dbo.tgt (id, amount) SELECT id, amount FROM dbo.src', N'_eu');
    EXEC (@sql);
END
GO
