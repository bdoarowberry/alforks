CREATE PROCEDURE dbo.load_target AS
BEGIN
    UPDATE v SET c = f.v FROM dbo.target_v v JOIN dbo.feed f ON f.k = v.k;
END
GO
