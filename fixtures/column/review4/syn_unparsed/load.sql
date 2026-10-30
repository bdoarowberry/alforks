CREATE TABLE dbo.target (k int, c int);
CREATE TABLE dbo.feed (k int, v varchar(20));
GO
CREATE SYNONYM dbo.target_alias FOR dbo.target;
GO
CREATE VIEW dbo.target_view AS SELECT k, c FROM dbo.target;
GO
CREATE PROCEDURE dbo.refresh_target AS
BEGIN
    UPDATE dbo.target_alias SET c = TRY_PARSE(f.v AS int USING 'en-US') FROM dbo.feed f WHERE f.k = dbo.target_alias.k;
    UPDATE dbo.target_view SET c = TRY_PARSE(f.v AS int USING 'de-DE') FROM dbo.feed f WHERE f.k = dbo.target_view.k;
END
GO
