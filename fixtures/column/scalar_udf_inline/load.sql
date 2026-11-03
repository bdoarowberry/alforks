CREATE TABLE dbo.region (region_code varchar(4), region_name varchar(40), weight int);
CREATE TABLE dbo.country (region_code varchar(4), country_name varchar(40));
CREATE TABLE dbo.fx_rate (currency char(3), rate_kind varchar(8), rate decimal(18,6));
CREATE TABLE dbo.sale (id int, code varchar(4), currency char(3), rate decimal(18,6), amount decimal(18,2), flag char(1));
CREATE TABLE dbo.report_tvf (id int, tvf_rate decimal(18,6));
CREATE TABLE dbo.lk_a (k int, a_val int);
CREATE TABLE dbo.lk_b (k int, b_val int);
CREATE TABLE dbo.src2 (id int, a int, b int, fname varchar(20), lname varchar(20), d varchar(10), e varchar(10));
CREATE TABLE dbo.report_more (id int, two_tvfs int, applied int, cte_first int, cte_after int, names varchar(60),
                              labels int);
CREATE TABLE dbo.report_versions (id int, altered varchar(40), swapped varchar(40), tvf_altered decimal(18,6));
CREATE TABLE dbo.report (id int, region varchar(40), rate decimal(18,6), wrong int, multi varchar(40),
                         nodefault varchar(40), looped int, outer_name varchar(60), twice varchar(40),
                         wrapped varchar(40), signed decimal(18,2), var_region varchar(40));
GO
CREATE PROCEDURE dbo.load_report
AS
BEGIN
    DECLARE @code varchar(4) = 'EMEA';
    INSERT INTO dbo.report (id, region, rate, wrong, multi, nodefault, looped, outer_name, twice, wrapped, signed, var_region)
    SELECT id,
           dbo.fn_region_name(code),
           dbo.fn_rate(currency, DEFAULT),
           dbo.fn_wrong(id),
           dbo.fn_multi(code),
           dbo.fn_nodefault(DEFAULT),
           dbo.fn_loop(id),
           dbo.fn_outer(code),
           dbo.fn_twice(code),
           ISNULL(UPPER(dbo.fn_region_name(code)), 'n/a'),
           amount * CASE WHEN flag = 'Y' THEN 1 ELSE -1 END,
           dbo.fn_region_name(@code)
    FROM dbo.sale;
    INSERT INTO dbo.report_versions (id, altered, swapped, tvf_altered)
    SELECT s.id, dbo.fn_altered(s.code), dbo.fn_swap(s.code, s.currency), a.rate
    FROM dbo.sale s CROSS APPLY dbo.tvf_altered(s.currency) a;
    INSERT INTO dbo.report_more (id, two_tvfs, names, labels)
    SELECT s.id, dbo.fn_via_a(s.a) + dbo.fn_via_b(s.b), ISNULL(s.fname, '') + ISNULL(s.lname, ''),
           TRY_CONVERT(int, s.d) + CONVERT(int, s.e) + LEN(RTRIM(s.fname)) + LEN(LTRIM(s.lname))
    FROM dbo.src2 s;
    INSERT INTO dbo.report_more (id, applied)
    SELECT s.id, x.v + y.v FROM dbo.src2 s CROSS APPLY dbo.tvf_a(s.a) x CROSS APPLY dbo.tvf_b(s.b) y;
    WITH c AS (SELECT s.id, s.b, dbo.fn_val(s.a) AS fv FROM dbo.src2 s)
    INSERT INTO dbo.report_more (id, cte_first, cte_after)
    SELECT c.id, dbo.fn_val(c.id) + c.b, c.fv + 1 FROM c;
    INSERT INTO dbo.report_tvf (id, tvf_rate)
    SELECT s.id, f.rate FROM dbo.sale s CROSS APPLY dbo.tvf_rates(s.currency, DEFAULT) f;
END
GO
