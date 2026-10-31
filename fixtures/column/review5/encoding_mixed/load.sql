-- load sizes � 2 (section sign typed in a Windows editor)
CREATE PROCEDURE dbo.load_out AS
INSERT INTO dbo.out (id, [Größe]) SELECT s.id, s.[Größe] FROM stage.src s;
GO
