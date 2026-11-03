CREATE TABLE dbo.region (region_code varchar(4), region_name varchar(40), weight int);
CREATE TABLE dbo.country (region_code varchar(4), country_name varchar(40));
CREATE TABLE dbo.fx_rate (currency char(3), rate_kind varchar(8), rate decimal(18,6));
CREATE TABLE dbo.sale (id int, code varchar(4), currency char(3), rate decimal(18,6), amount decimal(18,2), flag char(1));
CREATE TABLE dbo.report_tvf (id int, tvf_rate decimal(18,6));
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
    INSERT INTO dbo.report_tvf (id, tvf_rate)
    SELECT s.id, f.rate FROM dbo.sale s CROSS APPLY dbo.tvf_rates(s.currency, DEFAULT) f;
END
GO
