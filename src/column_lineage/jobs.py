"""SQL Server Agent job scripts (``--job-scripts``): which procedures each job step runs, and in what order.

Reads the scripts SSMS writes for "Script Job as CREATE" and similar hand-written ones:
``msdb.dbo.sp_add_job``, ``sp_add_jobstep``, ``sp_update_job`` (start step, enabled) and the schedule
procedures. Nothing is executed. A script is a static copy: whether it matches the job deployed in
production is not known, so any order taken from it is *inferred* and says so.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re

from table_lineage.index import mask_noncode
from table_lineage.sources import decode_source

AGENT_CALL = re.compile(
    r"\bEXEC(?:UTE)?\s+(?:@[\w$#@]+\s*=\s*)?(?:\[?msdb\]?\s*\.\s*)?(?:\[?dbo\]?\s*\.\s*|\.\s*)?"
    r"\[?(?P<proc>sp_add_job|sp_add_jobstep|sp_update_job|sp_update_jobstep|sp_add_jobschedule|sp_add_schedule|"
    r"sp_attach_schedule)\]?\b", re.I)
ARGUMENT = re.compile(r"\s*,?\s*@(?P<name>\w+)\s*=\s*(?P<value>N?'(?:[^']|'')*'|[-+\w@.$\[\]]+)(?:\s+OUTPUT\b)?",
                      re.I | re.S)
PROC_IN_COMMAND = re.compile(
    r"\bEXEC(?:UTE)?\s+(?:@[\w$#@]+\s*=\s*)?(?P<name>(?:\[[^\]]+\]|[\w$#]+)(?:\s*\.\s*(?:\[[^\]]+\]|[\w$#]*)){0,3})",
    re.I)


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
    path: str
    line: int

    def procedures(self) -> list[str]:
        """Procedure names the step's T-SQL command executes, in order (dynamic EXEC (...) is skipped)."""
        if self.subsystem.upper() != "TSQL":
            return []
        masked = mask_noncode(self.command)
        names = []
        for match in PROC_IN_COMMAND.finditer(masked):
            name = self.command[match.start("name"):match.end("name")]
            if name.lower().lstrip("[").startswith(("sp_executesql", "sys.sp_executesql")) or name.upper() == "AS":
                continue
            names.append(name)
        return names


@dataclass
class Job:
    name: str
    enabled: bool
    start_step_id: int
    path: str
    line: int
    steps: list[JobStep] = field(default_factory=list)
    schedules: list[dict] = field(default_factory=list)

    def sequence(self) -> list[JobStep]:
        """Steps in the order they run when each succeeds, from the start step to the first quit."""
        by_id = {s.step_id: s for s in self.steps}
        order, current, seen = [], by_id.get(self.start_step_id) or (min(self.steps, key=lambda s: s.step_id)
                                                                      if self.steps else None), set()
        while current is not None and current.step_id not in seen:
            seen.add(current.step_id)
            order.append(current)
            if current.on_success_action == 3:
                later = sorted(i for i in by_id if i > current.step_id)
                current = by_id.get(later[0]) if later else None
            elif current.on_success_action == 4:
                current = by_id.get(current.on_success_step_id)
            else:
                current = None
        return order


def _value(raw: str):
    raw = raw.strip()
    if raw[:2].upper() == "N'" or raw[:1] == "'":
        body = raw[raw.index("'") + 1:-1]
        return body.replace("''", "'")
    if re.fullmatch(r"[-+]?\d+", raw):
        return int(raw)
    return raw


def _arguments(text: str, start: int) -> dict:
    args, at = {}, start
    while True:
        match = ARGUMENT.match(text, at)
        if not match:
            return args
        args[match.group("name").lower()] = _value(match.group("value"))
        at = match.end()


def parse_job_scripts(paths: list[str]) -> tuple[list[Job], dict]:
    """Jobs found in the given files or directories (``*.sql`` under directories) and scan counts."""
    files: list[tuple[Path, str]] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            files.extend((f, f.relative_to(p).as_posix()) for f in sorted(p.rglob("*.sql")))
        else:
            files.append((p, p.name))
    jobs: list[Job] = []
    counts = {"files": 0, "jobs": 0, "steps": 0, "tsql_steps": 0, "ssis_steps": 0, "other_steps": 0}
    for path, rel in files:
        try:
            text, _ = decode_source(path.read_bytes())
        except OSError:
            continue
        counts["files"] += 1
        masked = mask_noncode(text)
        by_name: dict[str, Job] = {}
        by_variable: dict[str, Job] = {}
        current: Job | None = None
        for call in AGENT_CALL.finditer(masked):
            proc = call.group("proc").lower()
            args = _arguments(text, call.end())
            line = text.count("\n", 0, call.start()) + 1
            name = args.get("job_name")
            job = by_name.get(str(name).lower()) if isinstance(name, str) else None
            job_id = args.get("job_id")
            if job is None and isinstance(job_id, str) and job_id.startswith("@"):
                job = by_variable.get(job_id.lower())
            if proc == "sp_add_job":
                job = Job(str(name or f"job@{line}"), args.get("enabled", 1) != 0, int(args.get("start_step_id", 1)),
                          rel, line)
                jobs.append(job)
                by_name[job.name.lower()] = job
                if isinstance(job_id, str) and job_id.startswith("@"):
                    by_variable[job_id.lower()] = job
                current = job
                continue
            job = job or current
            if job is None:
                continue
            if proc == "sp_update_job":
                if "enabled" in args:
                    job.enabled = args["enabled"] != 0
                if "start_step_id" in args:
                    job.start_step_id = int(args["start_step_id"])
            elif proc == "sp_add_jobstep":
                step = JobStep(job, int(args.get("step_id", len(job.steps) + 1)), str(args.get("step_name", "")),
                               str(args.get("subsystem", "TSQL")), str(args.get("command", "")),
                               str(args["database_name"]) if args.get("database_name") else None,
                               int(args.get("on_success_action", 1)), int(args.get("on_success_step_id", 0)),
                               rel, line)
                job.steps.append(step)
                counts["steps"] += 1
                kind = step.subsystem.upper()
                counts["tsql_steps" if kind == "TSQL" else "ssis_steps" if kind == "SSIS" else "other_steps"] += 1
            elif proc in ("sp_add_jobschedule", "sp_add_schedule", "sp_attach_schedule"):
                job.schedules.append({k: v for k, v in args.items()
                                      if k in ("name", "schedule_name", "enabled", "freq_type", "freq_interval",
                                               "active_start_time", "freq_subday_type", "freq_subday_interval")})
    counts["jobs"] = len(jobs)
    return jobs, counts
