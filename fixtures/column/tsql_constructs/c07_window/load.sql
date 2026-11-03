CREATE TABLE dbo.ev_src (acct int, ev_ts datetime, amt int, ev_code varchar(5));
GO
CREATE TABLE dbo.win_tgt (acct int, prev_amt int, next_code varchar(5), first_amt int, run_total int, rn_amt int);
GO
CREATE PROCEDURE dbo.load_win AS
BEGIN
    INSERT INTO dbo.win_tgt (acct, prev_amt, next_code, first_amt, run_total, rn_amt)
    SELECT e.acct,
           LAG(e.amt, 1, -1) OVER (PARTITION BY e.acct ORDER BY e.ev_ts),
           LEAD(e.ev_code) OVER (PARTITION BY e.acct ORDER BY e.ev_ts),
           FIRST_VALUE(e.amt) OVER (PARTITION BY e.acct ORDER BY e.ev_ts ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW),
           SUM(e.amt) OVER (PARTITION BY e.acct ORDER BY e.ev_ts ROWS UNBOUNDED PRECEDING),
           0
    FROM dbo.ev_src AS e;
END
GO
CREATE PROCEDURE dbo.load_dedupe AS
BEGIN
    INSERT INTO dbo.win_tgt (acct, prev_amt, next_code, first_amt, run_total, rn_amt)
    SELECT d.acct, NULL, NULL, NULL, NULL, d.amt
    FROM (
        SELECT acct, amt, ROW_NUMBER() OVER (PARTITION BY acct ORDER BY ev_ts DESC) AS rn
        FROM dbo.ev_src
    ) AS d
    WHERE d.rn = 1;
END
GO
