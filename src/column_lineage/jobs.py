"""SQL Server Agent job scripts (``--job-scripts``): which procedures and packages each job step runs,
and in what order.

Reads the scripts SSMS writes for "Script Job as CREATE" and similar hand-written ones:
``msdb.dbo.sp_add_job``, ``sp_add_jobstep``, ``sp_update_job``, ``sp_update_jobstep``,
``sp_delete_job(step)``, ``sp_add_jobschedule``, ``sp_add_schedule`` + ``sp_attach_schedule``. Nothing
is executed. A script is a static copy: whether it matches the deployed job is not known, so an order
taken from it is *inferred*.

The reader is conservative. A job whose run is not fully understood (a step it cannot read, a loop, a
failure branch, an asynchronous package start, a step updated or deleted in a way it cannot follow, a
job defined twice, an argument it cannot parse) is marked with ``problems``; the tracer never orders
writers by such a job and reports the problem as the reason.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re

from table_lineage.index import mask_noncode
from table_lineage.sources import decode_source

AGENT_PROCS = ("sp_add_job", "sp_add_jobstep", "sp_update_job", "sp_update_jobstep", "sp_delete_job",
               "sp_delete_jobstep", "sp_add_jobschedule", "sp_add_schedule", "sp_attach_schedule",
               "sp_update_schedule")
AGENT_CALL = re.compile(
    r"\bEXEC(?:UTE)?\s+(?:@[\w$#@]+\s*=\s*)?(?:\[?msdb\]?\s*\.\s*)?(?:\[?dbo\]?\s*\.\s*|\.\s*)?"
    r"\[?(?P<proc>" + "|".join(AGENT_PROCS) + r")\]?(?![\w$#@])", re.I)
IDENT_PART = r"(?:\[(?:[^\]]|\]\])+\]|[\w$#]+)"
PROC_NAME = rf"{IDENT_PART}(?:\s*\.\s*(?:{IDENT_PART})?){{0,3}}"
# What a T-SQL job step may contain for its run to be followed: EXEC of a named procedure, USE, DECLARE /
# SET / SELECT of values, PRINT, WAITFOR, sp_start_job and the SSISDB execution procedures. Anything that
# writes data itself, branches, stops early or runs code chosen at run time makes the step opaque.
OPAQUE_TSQL = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|MERGE|TRUNCATE|INTO|BULK|OPENROWSET|OPENQUERY|IF|WHILE|GOTO|RETURN|THROW|"
    r"RAISERROR|TRY|CATCH|BEGIN|BREAK|CONTINUE|CURSOR|FETCH|sp_executesql|xp_cmdshell|"
    r"sp_stop_job)\b|\bEXEC(?:UTE)?\s*\(|\bEXEC(?:UTE)?\s+@[\w$#@]+(?!\s*=)", re.I)


def _int(value, default=None):
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"\s*[-+]?\d+\s*", value):
        return int(value)
    return default


@dataclass
class JobStep:
    job: "Job"
    step_id: int
    name: str
    subsystem: str
    command: str
    database: str | None
    on_success_action: int     # 1 quit with success, 2 quit with failure, 3 next step, 4 go to on_success_step_id
    on_success_step_id: int
    on_fail_action: int        # same codes, on failure
    on_fail_step_id: int
    path: str
    line: int

    @property
    def masked(self) -> str:
        return mask_noncode(self.command)

    def opaque_reason(self) -> str | None:
        """Why this step's effect cannot be followed, or None."""
        kind = self.subsystem.upper()
        if kind == "SSIS":
            return None if self.packages() else "ssis_step_package_not_read"
        if kind != "TSQL":
            return "step_subsystem_not_read"      # CmdExec, PowerShell, ...: may run anything
        if OPAQUE_TSQL.search(self.masked):
            return "step_command_not_followed"    # inline writes, branches, early exits, dynamic EXEC
        for _, kind_, _, _, extra in self.actions():
            if kind_ == "package" and not extra.get("synchronized"):
                return "asynchronous_package_execution"
            if kind_ == "start_job" and (extra.get("at_step") or extra.get("unknown")):
                return "job_start_not_followed"
        return None

    def actions(self) -> list[tuple[int, str, str, str | None, dict]]:
        """(offset, kind, name, database, extra) in command order: kind is "proc" (a procedure the
        command EXECs, resolved in the step's database or the last USE before it), "start_job" or
        "package" (SSISDB create_execution; extra["synchronized"] when SYNCHRONIZED is set to 1)."""
        if self.subsystem.upper() == "SSIS":
            return [(0, "package", p, None, {"synchronized": True}) for p in self.packages()]
        if self.subsystem.upper() != "TSQL":
            return []
        masked, out = self.masked, []
        uses = [(m.start(), self.command[m.start("db"):m.end("db")].strip("[]"))
                for m in re.finditer(rf"\bUSE\s+(?P<db>{IDENT_PART})", masked, re.I)]
        synchronized = bool(re.search(r"@parameter_name\s*=\s*N?'SYNCHRONIZED'[^;]*@parameter_value\s*=\s*1\b",
                                      self.command, re.I))
        for match in re.finditer(rf"\bEXEC(?:UTE)?\s+(?:@[\w$#@]+\s*=\s*)?(?P<name>{PROC_NAME})", masked, re.I):
            name = self.command[match.start("name"):match.end("name")].strip()
            last = re.split(r"\s*\.\s*", name)[-1].strip("[]").lower()
            if name.upper() == "AS" or last in ("start_execution", "set_execution_parameter_value"):
                continue
            database = next((db for at, db in reversed(uses) if at < match.start()), None) or self.database
            rest = self.command[match.end():]
            end = re.search(r";|\n\s*(?:EXEC|USE|DECLARE|SET|SELECT|PRINT|WAITFOR)\b", rest, re.I)
            args = rest[:end.start()] if end else rest
            if last == "sp_start_job":
                job = re.search(r"(?:@job_name\s*=\s*)?N?'((?:[^']|'')+)'", args, re.I)
                at_step = re.search(r"@step_name\s*=", args, re.I)
                out.append((match.start(), "start_job", job.group(1).replace("''", "'") if job else "",
                            None, {"at_step": bool(at_step), "unknown": job is None}))
            elif last == "create_execution":
                package = re.search(r"@package_name\s*=\s*N?'([^']+?)(?:\.dtsx)?'", args, re.I)
                project = re.search(r"@project_name\s*=\s*N?'([^']+)'", args, re.I)
                out.append((match.start(), "package", package.group(1).strip().lower() if package else "", None,
                            {"synchronized": synchronized, "project": project.group(1) if project else None}))
            else:
                out.append((match.start(), "proc", name, database, {}))
        return out

    def procedures(self) -> list[str]:
        return [name for _, kind, name, _, _ in self.actions() if kind == "proc"]

    def started_jobs(self) -> list[str]:
        """Names of jobs this step starts with msdb.dbo.sp_start_job (asynchronously; comments ignored)."""
        return [name for _, kind, name, _, _ in self.actions() if kind == "start_job" and name]

    def packages(self) -> list[str]:
        """Package names (file stem, lower case) the step runs."""
        if self.subsystem.upper() == "SSIS":
            found = [m.group(1).strip().lower() for m in re.finditer(r"([^\\/\"']+?)\.dtsx\b", self.command, re.I)]
            if not found:
                sql = re.search(r"/SQL\s+\"?\\?(?:[^\"\\]*\\)*([^\"\\]+)", self.command, re.I)
                if sql:
                    found.append(sql.group(1).strip().lower())
            return list(dict.fromkeys(found))
        return [name for _, kind, name, _, _ in self.actions() if kind == "package" and name]

    def package_project(self) -> str | None:
        """Project of the package the step runs: an /ISSERVER path (\\SSISDB\\folder\\project\\package.dtsx) or
        SSISDB create_execution @project_name."""
        match = re.search(r"\\SSISDB\\[^\\\"]+\\([^\\\"]+)\\[^\\\"]+\.dtsx", self.command, re.I)
        if match:
            return match.group(1)
        return next((extra.get("project") for _, kind, _, _, extra in self.actions()
                     if kind == "package" and extra.get("project")), None)


@dataclass
class Job:
    name: str
    enabled: bool
    start_step_id: int
    path: str
    line: int
    steps: list[JobStep] = field(default_factory=list)
    schedules: list[dict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)   # why this job's run cannot be followed

    def schedule_text(self) -> str:
        """Plain words for the job's enabled schedules (static script; not proof of the deployed ones)."""
        parts = []
        for sch in self.schedules:
            if _int(sch.get("enabled"), 1) == 0:
                continue
            if sch.get("unknown"):
                parts.append(f"shared schedule {sch.get('schedule_name') or '?'} (not in the scripts)")
                continue
            freq = {1: "once", 4: "daily", 8: "weekly", 16: "monthly", 32: "monthly (relative)",
                    64: "when SQL Agent starts", 128: "when the CPU is idle"}.get(_int(sch.get("freq_type")), "")
            start = _int(sch.get("active_start_time"))
            text = freq
            subday, interval = _int(sch.get("freq_subday_type")), _int(sch.get("freq_subday_interval"), 0)
            if subday in (2, 4, 8) and interval > 0:
                text += f", every {interval} {({2: 'seconds', 4: 'minutes', 8: 'hours'})[subday]}"
            if start is not None and freq not in ("when SQL Agent starts", "when the CPU is idle"):
                text += f" from {start // 10000:02d}:{start // 100 % 100:02d}"
            if text:
                parts.append(text)
        return "; ".join(parts)

    @property
    def scheduled(self) -> bool:
        return any(_int(sch.get("enabled"), 1) != 0 for sch in self.schedules)

    def _first(self) -> JobStep | None:
        by_id = {s.step_id: s for s in self.steps}
        return by_id.get(self.start_step_id) or (min(self.steps, key=lambda s: s.step_id) if self.steps else None)

    def _next(self, step: JobStep, action: int, target: int) -> JobStep | None:
        by_id = {s.step_id: s for s in self.steps}
        if action == 3:
            later = sorted(i for i in by_id if i > step.step_id)
            return by_id.get(later[0]) if later else None
        if action == 4:
            return by_id.get(target)
        return None

    def sequence(self) -> list[JobStep]:
        """Steps in the order they run when each succeeds, from the start step to the first quit. A loop
        is recorded as a problem (the run never settles)."""
        order, current, seen = [], self._first(), set()
        while current is not None:
            if current.step_id in seen:
                if "job_step_loop" not in self.problems:
                    self.problems.append("job_step_loop")
                break
            seen.add(current.step_id)
            order.append(current)
            current = self._next(current, current.on_success_action, current.on_success_step_id)
        return order

    def reachable(self) -> list[JobStep]:
        """Steps that can run on any path (success or failure), for run context."""
        first = self._first()
        found, frontier = [], [first] if first else []
        while frontier:
            step = frontier.pop()
            if any(step is f for f in found):
                continue
            found.append(step)
            for action, target in ((step.on_success_action, step.on_success_step_id),
                                   (step.on_fail_action, step.on_fail_step_id)):
                nxt = self._next(step, action, target)
                if nxt is not None:
                    frontier.append(nxt)
        return sorted(found, key=lambda s: s.step_id)


def _value(raw: str):
    raw = raw.strip()
    if re.fullmatch(r"(?:\s*N?'(?:[^']|'')*'\s*\+?)+", raw):
        return "".join(p.replace("''", "'") for p in re.findall(r"N?'((?:[^']|'')*)'", raw))   # N'a' + N'b'
    as_int = _int(raw)
    if as_int is not None:
        return as_int
    return raw


def _arguments(text: str, masked: str, start: int) -> tuple[dict, bool]:
    """Named arguments of one call: values are strings (concatenations joined), ints or raw text (a
    function call, a variable). The second result is False when the list could not be read to its end."""
    args, at = {}, start
    head = re.compile(r"\s*,?\s*@(?P<name>\w+)\s*=\s*")
    while True:
        match = head.match(masked, at)
        if not match:
            return args, not re.match(r"\s*,", masked[at:at + 200])
        i, depth = match.end(), 0
        while i < len(masked):
            ch = masked[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                if depth == 0:
                    break
                depth -= 1
            elif depth == 0 and (ch in ",;\n" or re.match(r"\s+OUTPUT\b", masked[i:i + 8], re.I)):
                break
            i += 1
        args[match.group("name").lower()] = _value(text[match.end():i])
        at = i
        output = re.match(r"\s+OUTPUT\b", masked[at:at + 8], re.I)
        if output:
            at += output.end()


def parse_job_scripts(paths: list[str]) -> tuple[list[Job], dict]:
    """Jobs found in the given files or directories (``*.sql`` under directories) and scan counts."""
    files: list[tuple[Path, str]] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            files.extend((f, f.relative_to(p).as_posix()) for f in sorted(p.rglob("*.sql")))
        else:
            files.append((p, p.name))
    calls = []   # (file index, rel, line, proc, args, complete)
    counts = {"files": 0, "jobs": 0, "steps": 0, "tsql_steps": 0, "ssis_steps": 0, "other_steps": 0,
              "jobs_not_followed": 0}
    for index, (path, rel) in enumerate(files):
        try:
            text, _ = decode_source(path.read_bytes())
        except OSError:
            continue
        counts["files"] += 1
        masked = mask_noncode(text)
        for call in AGENT_CALL.finditer(masked):
            args, complete = _arguments(text, masked, call.end())
            calls.append((index, rel, text.count("\n", 0, call.start()) + 1, call.group("proc").lower(), args,
                          complete))

    jobs: list[Job] = []
    by_name: dict[str, list[Job]] = {}
    shared: dict[str, dict] = {}
    variables: dict[tuple[int, str], Job] = {}
    created_at: dict[tuple[int, int], Job] = {}
    # Pass 1: jobs and shared schedules, so a later file can refer to them by name.
    for index, rel, line, proc, args, complete in calls:
        if proc == "sp_add_job":
            name = args.get("job_name")
            enabled = _int(args.get("enabled"), 1 if args.get("enabled") is None else None)
            job = Job(str(name or f"job@{rel}:{line}"), enabled != 0, _int(args.get("start_step_id"), 1) or 1,
                      rel, line)
            if not complete or enabled is None or (args.get("start_step_id") is not None
                                                   and _int(args["start_step_id"]) is None):
                job.problems.append("job_arguments_not_read")
            jobs.append(job)
            created_at[(index, line)] = job
            by_name.setdefault(job.name.lower(), []).append(job)
        elif proc == "sp_add_schedule":
            name = str(args.get("schedule_name") or args.get("name") or "")
            shared[name.lower()] = {k: v for k, v in args.items() if k in (
                "schedule_name", "enabled", "freq_type", "freq_interval", "active_start_time", "freq_subday_type",
                "freq_subday_interval")}
    for same in by_name.values():
        if len(same) > 1:
            for job in same:
                job.problems.append("job_defined_more_than_once")

    def job_for(index: int, args: dict, current: Job | None) -> Job | None:
        name = args.get("job_name")
        if isinstance(name, str):
            found = by_name.get(name.lower())
            return found[-1] if found else None
        job_id = args.get("job_id")
        if isinstance(job_id, str) and job_id.startswith("@"):
            return variables.get((index, job_id.lower()))
        return current if job_id is None else None

    # Pass 2: steps, updates, schedules, deletions.
    current: Job | None = None
    last_index = None
    for index, rel, line, proc, args, complete in calls:
        if index != last_index:
            current, last_index = None, index
        if proc == "sp_add_job":
            current = created_at.get((index, line))
            job_id = args.get("job_id")
            if current is not None and isinstance(job_id, str) and job_id.startswith("@"):
                variables[(index, job_id.lower())] = current   # bound in file order: scripts reuse @jobId
            continue
        if proc == "sp_add_schedule":
            continue
        job = job_for(index, args, current)
        if job is None:
            continue
        if not complete:
            job.problems.append("job_arguments_not_read")
        if proc == "sp_update_job":
            if "enabled" in args:
                enabled = _int(args["enabled"])
                if enabled is None:
                    job.problems.append("job_arguments_not_read")
                else:
                    job.enabled = enabled != 0
            if "start_step_id" in args:
                start = _int(args["start_step_id"])
                if start is None:
                    job.problems.append("job_arguments_not_read")
                else:
                    job.start_step_id = start
        elif proc == "sp_delete_job":
            job.problems.append("job_deleted_in_scripts")
        elif proc == "sp_add_jobstep":
            raw_id = args.get("step_id")
            step_id = _int(raw_id, len(job.steps) + 1 if raw_id is None else None)
            numbers = [_int(args.get(k), d if args.get(k) is None else None)
                       for k, d in (("on_success_action", 1), ("on_success_step_id", 0),
                                    ("on_fail_action", 2), ("on_fail_step_id", 0))]
            command = args.get("command", "")
            if step_id is None or any(n is None for n in numbers) or not isinstance(command, str) or (
                    command and isinstance(args.get("command"), str) and command.lstrip().startswith("@")):
                job.problems.append("job_arguments_not_read")
            step_id = step_id if step_id is not None else len(job.steps) + 1
            numbers = [n if n is not None else 0 for n in numbers]
            step = JobStep(job, step_id, str(args.get("step_name", "")), str(args.get("subsystem", "TSQL")),
                           str(command), str(args["database_name"]) if args.get("database_name") else None,
                           numbers[0], numbers[1], numbers[2], numbers[3], rel, line)
            job.steps.append(step)
            counts["steps"] += 1
            kind = step.subsystem.upper()
            counts["tsql_steps" if kind == "TSQL" else "ssis_steps" if kind == "SSIS" else "other_steps"] += 1
        elif proc == "sp_update_jobstep":
            step_id = _int(args.get("step_id"))
            step = next((s for s in job.steps if s.step_id == step_id), None)
            if step is None:
                job.problems.append("job_step_update_not_followed")
                continue
            for key, attr in (("command", "command"), ("subsystem", "subsystem"), ("database_name", "database"),
                              ("step_name", "name")):
                if key in args:
                    setattr(step, attr, str(args[key]))
            for key, attr in (("on_success_action", "on_success_action"), ("on_success_step_id", "on_success_step_id"),
                              ("on_fail_action", "on_fail_action"), ("on_fail_step_id", "on_fail_step_id")):
                if key in args:
                    value = _int(args[key])
                    if value is None:
                        job.problems.append("job_arguments_not_read")
                    else:
                        setattr(step, attr, value)
        elif proc == "sp_delete_jobstep":
            job.problems.append("job_step_deleted_in_scripts")   # later steps are renumbered
        elif proc == "sp_add_jobschedule":
            job.schedules.append({k: v for k, v in args.items() if k in (
                "name", "enabled", "freq_type", "freq_interval", "active_start_time", "freq_subday_type",
                "freq_subday_interval")})
        elif proc == "sp_attach_schedule":
            name = str(args.get("schedule_name") or "")
            schedule = shared.get(name.lower())
            job.schedules.append(dict(schedule) if schedule else {"schedule_name": name or "?", "unknown": True})
        elif proc == "sp_update_schedule":
            job.problems.append("schedule_updated_in_scripts")

    for job in jobs:
        for step in job.steps:
            if step.subsystem.upper() == "TSQL" and step.database is None:
                step.database = "master"   # SQL Agent runs a T-SQL step without @database_name in master
        job.sequence()   # records a loop
        if any(s.on_fail_action in (3, 4) for s in job.steps):
            job.problems.append("job_has_failure_branches")
        job.problems = list(dict.fromkeys(job.problems))
    counts["jobs"] = len(jobs)
    counts["jobs_not_followed"] = sum(1 for j in jobs if j.problems)
    return jobs, counts
