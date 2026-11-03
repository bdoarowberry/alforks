-- Invented acceptance case for --scenario.
CREATE TABLE dbo.regular_stage (value_col int);
CREATE TABLE dbo.interim_stage (value_col int);
CREATE TABLE dbo.saved_result (value_col int);
CREATE TABLE dbo.raw_feed (value_col int);
GO
CREATE PROC dbo.publish_data
    @mode bit = 0,
    @rebuild bit = 0
AS
BEGIN
    IF @rebuild = 0
    BEGIN
        SELECT value_col FROM dbo.saved_result;
        RETURN;
    END;

    IF @mode = 0
        INSERT dbo.saved_result(value_col)
            SELECT value_col FROM dbo.regular_stage;
    ELSE
        INSERT dbo.saved_result(value_col)
            SELECT value_col FROM dbo.interim_stage;
END;
GO
-- The staging tables are loaded by separate scheduled procedures, not called from publish_data.
CREATE PROC dbo.load_regular_stage AS
    INSERT dbo.regular_stage(value_col) SELECT value_col FROM dbo.raw_feed;
GO
CREATE PROC dbo.load_interim_stage AS
    INSERT dbo.interim_stage(value_col) SELECT value_col * 2 FROM dbo.raw_feed;
GO
