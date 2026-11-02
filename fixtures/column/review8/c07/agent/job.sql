DECLARE @jobId BINARY(16)
EXEC msdb.dbo.sp_add_job @job_name=N'J', @enabled=1, @job_id = @jobId OUTPUT
EXEC msdb.dbo.sp_add_jobstep @job_id=@jobId, @step_name=N'one', @step_id=1, @on_success_action=3, @subsystem=N'TSQL', @command=N'EXEC dbo.w_one', @database_name=N'demo'
EXEC msdb.dbo.sp_add_jobstep @job_id=@jobId, @step_name=N'two', @step_id=2, @on_success_action=3, @subsystem=N'TSQL', @command=N'EXEC dbo.w_two', @database_name=N'demo'
EXEC msdb.dbo.sp_add_jobstep @job_id=@jobId, @step_name=N'three', @step_id=3, @on_success_action=1, @subsystem=N'CmdExec', @command=N'sqlcmd -S . -d demo -Q "EXEC dbo.w_one"'
EXEC msdb.dbo.sp_add_jobschedule @job_id=@jobId, @name=N'Daily', @enabled=1, @freq_type=4, @freq_interval=1, @active_start_time=10000
GO
