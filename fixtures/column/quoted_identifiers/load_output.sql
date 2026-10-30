CREATE PROCEDURE [demo].[Load_Output]
AS
BEGIN
    SELECT [s].[Input_A] AS [Staged_Value], s.ID
    INTO #Stage
    FROM [DEMO].[SOURCE] AS [s];

    INSERT INTO [demo].[Output] ([ID], [Result_Value])
    SELECT st.id, st.[staged_value]
    FROM #STAGE AS st;
END;
GO
