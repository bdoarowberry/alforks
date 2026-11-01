SET ANSI_NULLS ON
GO
SET QUOTED_IDENTIFIER ON
GO
CREATE TABLE [dbo].[SourceTable](
	[id] [int] IDENTITY(1,1) NOT NULL,
	[col_a] [nvarchar](max) NULL,
	[notes] [xml] NULL,
 CONSTRAINT [PK_SourceTable] PRIMARY KEY CLUSTERED
(
	[id] ASC
)WITH (PAD_INDEX = OFF, STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, ALLOW_ROW_LOCKS = ON, ALLOW_PAGE_LOCKS = ON) ON [PRIMARY]
) ON [PRIMARY] TEXTIMAGE_ON [PRIMARY]
GO
CREATE TABLE [dbo].[TargetTable](
	[col_a] [nvarchar](max) NULL,
	[col_b] [int] SPARSE NULL,
	[secret] [nvarchar](20) MASKED WITH (FUNCTION = 'default()') NULL
) ON [PRIMARY] TEXTIMAGE_ON [PRIMARY]
GO
CREATE TABLE [dbo].[Indexed](
	[col_a] [int] NULL,
	INDEX [IX_Indexed] NONCLUSTERED ([col_a]),
	[col_b] [int] NULL
) ON [PRIMARY]
GO
CREATE TABLE dbo.Timed (
	id int NOT NULL,
	col_a int NULL,
	valid_from datetime2 GENERATED ALWAYS AS ROW START NOT NULL,
	valid_to datetime2 GENERATED ALWAYS AS ROW END NOT NULL,
	PERIOD FOR SYSTEM_TIME (valid_from, valid_to)
) WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE = dbo.Timed_History));
GO
CREATE TABLE dbo.Feed (col_a nvarchar(max), col_b int, secret nvarchar(20), id int);
CREATE TABLE dbo.Out_cte (col_a nvarchar(max));
GO
