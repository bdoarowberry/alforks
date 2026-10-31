CREATE PROCEDURE dbo.load_out2 AS
UPDATE o SET c_shadow = dbo.fn_shadow(s.code),
             c_nested = dbo.fn_nested(s.code),
             c_schemabound = CASE WHEN s.flag = 1 THEN 'A' ELSE 'B' END
FROM dbo.out o JOIN stage.src s ON s.id = o.id;
GO
