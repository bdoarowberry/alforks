USE [msdb]
GO
DECLARE @jobId BINARY(16)
EXEC msdb.dbo.sp_add_job @job_name=N'Chain parent', @enabled=1, @job_id = @jobId OUTPUT
EXEC msdb.dbo.sp_add_jobstep @job_id=@jobId, @step_name=N'Load', @step_id=1, @on_success_action=3,
		@subsystem=N'TSQL', @command=N'EXEC dbo.chain_load', @database_name=N'demo'
EXEC msdb.dbo.sp_add_jobstep @job_id=@jobId, @step_name=N'Start child', @step_id=2, @on_success_action=1,
		@subsystem=N'TSQL', @command=N'EXEC msdb.dbo.sp_start_job @job_name = N''Chain child''', @database_name=N'msdb'
EXEC msdb.dbo.sp_add_jobschedule @job_id=@jobId, @name=N'Nightly', @enabled=1, @freq_type=4, @freq_interval=1,
		@active_start_time=10000
GO
DECLARE @jobId BINARY(16)
EXEC msdb.dbo.sp_add_job @job_name=N'Chain child', @enabled=1, @job_id = @jobId OUTPUT
EXEC msdb.dbo.sp_add_jobstep @job_id=@jobId, @step_name=N'Fix', @step_id=1, @on_success_action=1,
		@subsystem=N'TSQL', @command=N'EXEC dbo.chain_fix', @database_name=N'demo'
GO
DECLARE @jobId BINARY(16)
EXEC msdb.dbo.sp_add_job @job_name=N'Overlap parent', @enabled=1, @job_id = @jobId OUTPUT
EXEC msdb.dbo.sp_add_jobstep @job_id=@jobId, @step_name=N'Start child', @step_id=1, @on_success_action=3,
		@subsystem=N'TSQL', @command=N'EXEC msdb.dbo.sp_start_job N''Overlap child''', @database_name=N'msdb'
EXEC msdb.dbo.sp_add_jobstep @job_id=@jobId, @step_name=N'Fix', @step_id=2, @on_success_action=1,
		@subsystem=N'TSQL', @command=N'EXEC dbo.ov_fix', @database_name=N'demo'
GO
DECLARE @jobId BINARY(16)
EXEC msdb.dbo.sp_add_job @job_name=N'Overlap child', @enabled=1, @job_id = @jobId OUTPUT
EXEC msdb.dbo.sp_add_jobstep @job_id=@jobId, @step_name=N'Load', @step_id=1, @on_success_action=1,
		@subsystem=N'TSQL', @command=N'EXEC dbo.ov_load', @database_name=N'demo'
GO
