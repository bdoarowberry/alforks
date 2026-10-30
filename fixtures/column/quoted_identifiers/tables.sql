SET QUOTED_IDENTIFIER ON;
GO

CREATE TABLE [Demo].[Source] (
    [ID] int NOT NULL,
    [Input_A] int NULL
);
GO

CREATE TABLE "demo"."OUTPUT" (
    id int NOT NULL,
    RESULT_VALUE int NULL
);
GO
