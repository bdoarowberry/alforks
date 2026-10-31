CREATE PROCEDURE dbo.load_out AS
INSERT INTO dbo.out (id, c_session, c_cond, c_threepart, c_bracket, c_prefix, c_decimal, c_now)
SELECT s.id,
       dbo.fn_session(s.code),
       dbo.fn_cond(s.flag, s.code),
       otherdb.dbo.fn_pure(s.code),
       [dbo].[fn_bracket](s.code),
       dbo.fn_prefix(s.d, s.date2),
       dbo.fn_decimal(s.amount, DEFAULT),
       dbo.fn_now(s.code)
FROM stage.src s;
GO
