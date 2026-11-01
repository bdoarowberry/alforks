CREATE PROCEDURE dbo.rescale AS
    UPDATE dbo.Scaled SET amount = amount * 2;
GO
CREATE PROCEDURE dbo.set_flags AS
    UPDATE f SET flag = s.flag
    FROM dbo.Flags AS f
    JOIN dbo.FlagSource AS s ON s.id = f.id;
GO
CREATE PROCEDURE dbo.load_doubled AS
BEGIN
    INSERT INTO dbo.Doubled (id, amount) SELECT id, amount FROM dbo.Raw;
    UPDATE dbo.Doubled SET amount = amount * 2;
END
GO
CREATE PROCEDURE dbo.build_labels AS
BEGIN
    DECLARE @s varchar(400) = '';
    SELECT @s = @s + part FROM dbo.Parts;
    INSERT INTO dbo.Labels (id, label) VALUES (1, @s);
END
