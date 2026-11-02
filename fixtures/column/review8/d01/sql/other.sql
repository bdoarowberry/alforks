USE other;
GO
CREATE TABLE dbo.Junk (id int);
GO
CREATE PROCEDURE dbo.w_one AS
    INSERT INTO dbo.Junk (id) VALUES (1);
GO
