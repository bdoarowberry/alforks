CREATE PROCEDURE dbo.load_dest AS
INSERT INTO dbo.dest (ref_no) SELECT s.source_ref FROM demo.stage.src s
GO
