USE [msdb]
GO
BEGIN TRANSACTION
DECLARE @ReturnCode INT
SELECT @ReturnCode = 0
DECLARE @jobId BINARY(16)
EXEC @ReturnCode =  msdb.dbo.sp_add_job @job_name=N'Nightly load',
		@enabled=1,
		@notify_level_eventlog=0,
		@description=N'Invented job.',
		@category_name=N'[Uncategorized (Local)]',
		@owner_login_name=N'sa', @job_id = @jobId OUTPUT
IF (@@ERROR <> 0 OR @ReturnCode <> 0) GOTO QuitWithRollback
EXEC @ReturnCode = msdb.dbo.sp_add_jobstep @job_id=@jobId, @step_name=N'Load',
		@step_id=1,
		@cmdexec_success_code=0,
		@on_success_action=3,
		@on_success_step_id=0,
		@on_fail_action=2,
		@subsystem=N'TSQL',
		@command=N'EXEC dbo.load_target;',
		@database_name=N'demo',
		@flags=0
IF (@@ERROR <> 0 OR @ReturnCode <> 0) GOTO QuitWithRollback
EXEC @ReturnCode = msdb.dbo.sp_add_jobstep @job_id=@jobId, @step_name=N'Fix a',
		@step_id=2,
		@on_success_action=3,
		@subsystem=N'TSQL',
		@command=N'EXEC dbo.fix_a @mode = ''full'';',
		@database_name=N'demo'
IF (@@ERROR <> 0 OR @ReturnCode <> 0) GOTO QuitWithRollback
EXEC @ReturnCode = msdb.dbo.sp_add_jobstep @job_id=@jobId, @step_name=N'Run all',
		@step_id=3,
		@on_success_action=1,
		@subsystem=N'TSQL',
		@command=N'EXEC dbo.run_all',
		@database_name=N'demo'
IF (@@ERROR <> 0 OR @ReturnCode <> 0) GOTO QuitWithRollback
EXEC @ReturnCode = msdb.dbo.sp_update_job @job_id = @jobId, @start_step_id = 1
EXEC @ReturnCode = msdb.dbo.sp_add_jobschedule @job_id=@jobId, @name=N'Daily', @enabled=1, @freq_type=4,
		@freq_interval=1, @active_start_time=20000
COMMIT TRANSACTION
GOTO EndSave
QuitWithRollback:
    IF (@@TRANCOUNT > 0) ROLLBACK TRANSACTION
EndSave:
GO
