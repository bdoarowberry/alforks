CREATE TABLE dbo.dflt_tgt (
    id int IDENTITY(1,1) NOT NULL,
    status_cd varchar(10) NOT NULL DEFAULT ('NEW'),
    note varchar(50) NULL
);
GO
CREATE PROCEDURE dbo.load_default_kw AS
BEGIN
    INSERT INTO dbo.dflt_tgt (status_cd, note) VALUES (DEFAULT, 'kw');
END
GO
