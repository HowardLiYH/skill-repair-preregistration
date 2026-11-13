"""Disk/time monitoring for a single staged trial process tree."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time
from .contracts import write_json_atomic


def guarded_launch(command, *, cwd, logs, container_names, timeout_seconds,
                   minimum_free_bytes, disk_paths, interval_seconds=2):
    if type(timeout_seconds) is not int or timeout_seconds<=0 or type(minimum_free_bytes) is not int or minimum_free_bytes<=0:
        raise ValueError('Positive explicit launch disk/time bounds required')
    if any(not re.fullmatch(r'[a-zA-Z0-9_.-]+', name) for name in container_names):
        raise ValueError('Explicit task-owned container names required')
    logs=Path(logs);logs.mkdir(parents=True,exist_ok=True)
    paths=[Path(p) for p in disk_paths]
    if not paths:raise ValueError('Disk monitoring paths required')
    status={'timeout_seconds':timeout_seconds,'minimum_free_bytes_required':minimum_free_bytes,
            'minimum_free_bytes_observed':None,'container_names':list(container_names),'interrupted_reason':None,
            'returncode':None,'monitor_interval_seconds':interval_seconds}
    process=None;started=time.monotonic()
    try:
        with (logs/'launcher.stdout').open('w') as stdout,(logs/'launcher.stderr').open('w') as stderr, (logs/'disk_monitor.jsonl').open('w') as disk:
            while True:
                readings={str(p):shutil.disk_usage(p).free for p in paths};free=min(readings.values())
                previous=status['minimum_free_bytes_observed'];status['minimum_free_bytes_observed']=free if previous is None else min(previous,free)
                elapsed=time.monotonic()-started
                disk.write(json.dumps({'elapsed_seconds':elapsed,'free_bytes':readings})+'\n');disk.flush()
                if free<minimum_free_bytes:
                    status['interrupted_reason']='disk_reserve_reached';raise RuntimeError('Trial stopped at disk reserve')
                if elapsed>timeout_seconds:
                    status['interrupted_reason']='wall_time_limit';raise RuntimeError('Trial time limit reached')
                if process is None:
                    process=subprocess.Popen(command,cwd=cwd,stdout=stdout,stderr=stderr,start_new_session=True)
                code=process.poll()
                if code is not None:
                    status['returncode']=code;return subprocess.CompletedProcess(command,code)
                time.sleep(interval_seconds)
    except BaseException:
        if process is not None and process.poll() is None:
            try:os.killpg(process.pid,signal.SIGTERM)
            except ProcessLookupError:pass
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                process.wait(timeout=5)
        # Kill the client tree FIRST so the staged harness cannot recreate a
        # container after its cleanup. Names are computed from this trial only.
        if process is not None:
            status['cleanup']=[]
            for name in container_names:
                try:
                    result=subprocess.run(['docker','rm','-f',name],capture_output=True,timeout=30)
                    status['cleanup'].append({'container':name,'returncode':result.returncode})
                except (OSError,subprocess.SubprocessError) as error:
                    status['cleanup'].append({'container':name,'error_type':type(error).__name__})
        raise
    finally:
        status['duration_seconds']=time.monotonic()-started
        write_json_atomic(logs/'launcher_guard.json',status)
