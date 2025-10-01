#!/usr/bin/env python
# Ansible callback plugin to send full playbook stdout to the dashboard API

from __future__ import annotations

import os
import json
from pathlib import Path
from ansible.plugins.callback import CallbackBase

try:
    # stdlib http to avoid external deps
    from urllib.request import Request, urlopen
    from urllib.error import URLError, HTTPError
except Exception:  # pragma: no cover
    Request = None  # type: ignore
    urlopen = None  # type: ignore
    URLError = HTTPError = Exception  # type: ignore

DOCUMENTATION = r'''
callback: dashboard_log
type: notification
short_description: Send full Ansible stdout to Ansible Job Dashboard
description:
  - Streams Ansible stdout to the dashboard as lines are produced, with an end-of-run fallback upload.
    - Reads job identifiers from the DASHBOARD_JOB_ID environment variable or runtime stats when available.
  - Allows dashboard connection details to be supplied via environment variables or ansible.cfg.
    - Designed to operate without writing to disk, making it suitable for read-only control nodes.
    - Supports project-level defaults via a `dashboard.env` file located alongside the plugin.
requirements:
  - none (uses Python stdlib)
options:
  dashboard_url:
    description:
      - Base URL for the Ansible Job Dashboard API.
    env:
      - name: DASHBOARD_URL
    ini:
      - section: callback_dashboard_log
        key: url
      - section: defaults
        key: dashboard_url
    type: str
    default: http://localhost:8000
  dashboard_job_name:
    description:
      - Default job name to use when a play does not provide one.
    env:
      - name: DASHBOARD_JOB_NAME
    ini:
      - section: callback_dashboard_log
        key: job_name
    type: str
  dashboard_scope:
    description:
      - Default scope string to associate with the job when discovery is not possible.
    env:
      - name: DASHBOARD_SCOPE
    ini:
      - section: callback_dashboard_log
        key: scope
    type: str
  dashboard_triggered_by:
    description:
      - Default "triggered by" identity for the job, if one cannot be derived.
    env:
      - name: DASHBOARD_TRIGGERED_BY
    ini:
      - section: callback_dashboard_log
        key: triggered_by
    type: str
  dashboard_log_file:
    description:
      - Path to the log file used as a fallback when streaming is unavailable.
    env:
      - name: DASHBOARD_LOG_FILE
    ini:
      - section: callback_dashboard_log
        key: log_file
      - section: defaults
        key: dashboard_log_file
    type: path
    default: ./ansible.last.log
'''
CALLBACK_VERSION = 2.0
CALLBACK_TYPE = 'notification'
CALLBACK_NAME = 'dashboard_log'

class CallbackModule(CallbackBase):
    def __init__(self):  # noqa: D401
        super().__init__()
        # Settings
        self._env_settings = self._load_env_file()
        self.dashboard_url = self._get_setting('DASHBOARD_URL', 'http://localhost:8000')
        self.log_file = self._get_setting('DASHBOARD_LOG_FILE', './ansible.last.log')
        self.job_id = None
        self._buffer = []  # fallback buffer if log_file is missing
        self._pending_lines = []
        self._sent_any = False
        self._job_started = False
        self._progress_total = 0
        self._tasks_started = 0
        self._last_progress_sent = 0
        self._seen_play_uids = set()
        self._failed = False
        self._job_name_override = self._get_setting('DASHBOARD_JOB_NAME')
        self._scope_override = self._get_setting('DASHBOARD_SCOPE')
        self._trigger_override = self._get_setting('DASHBOARD_TRIGGERED_BY')
        self.playbook_dir = None
        self._options_applied = False

    def set_options(self, task_keys=None, var_options=None, direct=None):  # noqa: D401
        super().set_options(task_keys=task_keys, var_options=var_options, direct=direct)
        try:
            url = self.get_option('dashboard_url')
            if url:
                self.dashboard_url = url
        except Exception:
            pass
        try:
            log_file = self.get_option('dashboard_log_file')
            if log_file:
                self.log_file = log_file
        except Exception:
            pass
        try:
            job_name = self.get_option('dashboard_job_name')
            if job_name:
                self._job_name_override = job_name
        except Exception:
            pass
        try:
            scope = self.get_option('dashboard_scope')
            if scope:
                self._scope_override = scope
        except Exception:
            pass
        try:
            triggered_by = self.get_option('dashboard_triggered_by')
            if triggered_by:
                self._trigger_override = triggered_by
        except Exception:
            pass
        self._options_applied = True

    def v2_playbook_on_start(self, playbook):
        fn = None
        try:
            # Older/newer ansible may use _file_name
            if hasattr(playbook, '_file_name') and getattr(playbook, '_file_name'):
                fn = getattr(playbook, '_file_name')
            # Some versions expose .filename
            if not fn and hasattr(playbook, 'filename') and getattr(playbook, 'filename'):
                fn = getattr(playbook, 'filename')
            # Fallback to loader basedir
            if not fn and hasattr(playbook, '_loader') and hasattr(playbook._loader, 'get_basedir'):
                basedir = playbook._loader.get_basedir()
                if basedir:
                    self.playbook_dir = str(Path(basedir).resolve())
            if fn and not self.playbook_dir:
                self.playbook_dir = str(Path(fn).resolve().parent)
        except Exception:
            self.playbook_dir = None
        self.job_id = None
        self._job_started = False
        self._failed = False
        self._progress_total = 0
        self._tasks_started = 0
        self._last_progress_sent = 0
        self._seen_play_uids = set()
        self._buffer = []
        self._pending_lines = []
        self._sent_any = False

    def v2_playbook_on_play_start(self, play):
        self._maybe_update_dashboard_url(play)
        self._ensure_job_started(play)
        self._accumulate_total_tasks(play)
        try:
            name = play.get_name().strip()
        except Exception:
            name = ''
        if not name:
            name = 'Play'
        # PLAY header
        self._emit(f"\nPLAY [{name}] {'*'*73}")

    def v2_playbook_on_stats(self, stats):
        # First, try to pull job_id and dashboard_url from custom stats (set via set_stats)
        try:
            custom_job_id = self._get_custom_stat(stats, 'os_updates_job_id') or self._get_custom_stat(stats, 'job_id')
            if custom_job_id:
                self._update_job_id(custom_job_id)
            custom_url = self._get_custom_stat(stats, 'dashboard_url')
            if custom_url:
                self.dashboard_url = str(custom_url).strip()
        except Exception:
            pass

        # Append a simple recap similar to Ansible's default output
        try:
            self._emit("\nPLAY RECAP " + "*"*69)
            processed = sorted(getattr(stats, 'processed', {}).keys())
            for host in processed:
                s = stats.summarize(host)
                line = (
                    f"{host:22} : ok={s.get('ok',0)}   changed={s.get('changed',0)}    "
                    f"unreachable={s.get('unreachable',0)}    failed={s.get('failures',0)}    "
                    f"skipped={s.get('skipped',0)}    rescued={s.get('rescued',0)}    ignored={s.get('ignored',0)}  "
                )
                self._emit(line)
        except Exception:
            pass
        # Determine log content (prefer our generated console-style buffer)
        log_text = "\n".join(self._buffer) or self._read_log_file()

        # Refresh job_id at the end using all mechanisms (custom stats > file > env)
        if not self.job_id:
            self._update_job_id(self._discover_job_id())

        # Flush any queued incremental lines now that the run is ending.
        self._flush_pending_lines()

        job_id = self._ensure_job_id()

        # If streaming never happened (e.g. job_id appeared very late), fall back to
        # sending the combined log output once.
        if not self._sent_any and log_text and job_id:
            self._post_log_chunks(job_id, log_text)

        # Always mark completion if we managed to create a job.
        if job_id:
            # Ensure progress hits 100% before completion signalling.
            self._post_progress(job_id, progress=100)
            self._last_progress_sent = 100
            status = 'failed' if self._failed else 'success'
            message = 'Playbook completed with failures.' if self._failed else 'Playbook completed successfully.'
            self._post_completion(job_id, status=status, message=message)

        self._buffer = []
        self._job_started = False

    # Minimal capture of task events when log file is not present
    def v2_playbook_on_task_start(self, task, is_conditional):
        title = task.get_name().strip()
        self._emit(f"\nTASK [{title}] {'*'*74}")
        self._record_task_start(task)

    def v2_runner_on_ok(self, result):
        host = result._host.get_name()
        deleg = self._delegate_suffix(result)
        if result.is_changed():
            self._emit(f"changed: [{host}{deleg}]")
        else:
            self._emit(f"ok: [{host}{deleg}]")

    def v2_runner_on_failed(self, result, ignore_errors=False):
        host = result._host.get_name()
        deleg = self._delegate_suffix(result)
        self._emit(f"fatal: [{host}{deleg}] => {self._short_result(result)}")
        if not ignore_errors:
            self._failed = True
        detail = self._format_failure_detail(result)
        if detail:
            try:
                self._buffer.append(detail)
            except Exception:
                pass
            self._queue_message(detail, level='error')

    def v2_runner_on_skipped(self, result):
        host = result._host.get_name()
        deleg = self._delegate_suffix(result)
        self._emit(f"skipping: [{host}{deleg}]")

    def v2_runner_on_unreachable(self, result):
        host = result._host.get_name()
        deleg = self._delegate_suffix(result)
        self._emit(f"unreachable: [{host}{deleg}] => {self._short_result(result)}")
        self._failed = True
        detail = self._format_failure_detail(result, prefix="Host unreachable")
        if detail:
            try:
                self._buffer.append(detail)
            except Exception:
                pass
            self._queue_message(detail, level='error')

    def v2_playbook_on_include(self, included_file):
        try:
            msg = f"included: {included_file._filename} for {', '.join([h.name for h in included_file._hosts])}"
            self._emit(msg)
        except Exception:
            pass

    # Helpers
    def _discover_job_id(self):
        try:
            value = self._get_setting('DASHBOARD_JOB_ID')
            if value:
                return int(str(value).strip())
        except Exception:
            return None
        return None

    def _find_upwards(self, name: str) -> Path | None:
        try:
            cur = Path.cwd().resolve()
            root = cur.anchor
            while True:
                cand = cur / name
                if cand.exists():
                    return cand
                if str(cur) == root:
                    break
                cur = cur.parent
        except Exception:
            return None
        return None

    def _read_log_file(self):
        # Try configured path, then playbook_dir/ansible.last.log, then search upwards by filename
        try:
            # 1) Direct path
            p = Path(self.log_file)
            if p.exists():
                return p.read_text(errors='ignore')
            # 2) If we know the playbook dir, try its parent (likely ansible project dir)
            if getattr(self, 'playbook_dir', None):
                try:
                    parent = Path(self.playbook_dir).resolve().parent
                    cand = parent / Path(self.log_file).name
                    if cand.exists():
                        return cand.read_text(errors='ignore')
                except Exception:
                    pass
            # 3) Search upwards for a file with the right name
            up = self._find_upwards(Path(self.log_file).name)
            if up and up.exists():
                return up.read_text(errors='ignore')
        except Exception:
            return None
        return None

    def _post_log_chunks(self, job_id: int, text: str, chunk_size: int = 7000):
        # Split the text into chunks and post as progress messages
        i = 0
        length = len(text)
        while i < length:
            chunk = text[i:i+chunk_size]
            i += chunk_size
            self._post_progress(job_id, chunk, level="info")

    def _post_json(self, url: str, payload: dict, expect_json: bool = False):
        try:
            data = json.dumps(payload).encode('utf-8')
            req = Request(url, data=data, headers={'Content-Type': 'application/json'}, method='POST')
            with urlopen(req, timeout=15) as resp:
                content = resp.read()
                if expect_json:
                    if not content:
                        return {}
                    try:
                        return json.loads(content.decode('utf-8'))
                    except Exception:
                        return {}
                return None
        except (URLError, HTTPError, Exception):
            # Swallow errors to not break the play
            return None

    def _post_progress(self, job_id: int, text: str | None = None, level: str = "info", progress: int | None = None):
        if text is None and progress is None:
            return
        payload = {
            "job_id": int(job_id),
        }
        if text is not None:
            payload["message"] = text
        if level and (text is not None or level.lower() != "info"):
            payload["level"] = level
        if progress is not None:
            try:
                payload["progress"] = max(0, min(100, int(progress)))
            except Exception:
                pass
        self._sent_any = True
        self._post_json(self._api_url('/api/jobs/progress'), payload)

    def _update_job_id(self, value):
        if value is None:
            return
        try:
            candidate = int(str(value).strip())
        except Exception:
            return
        if self.job_id == candidate:
            return
        self.job_id = candidate
        self._flush_pending_lines()

    def _ensure_job_id(self):
        if self.job_id:
            return self.job_id
        refreshed = self._discover_job_id()
        if refreshed is not None:
            try:
                self.job_id = int(str(refreshed).strip())
            except Exception:
                self.job_id = None
        return self.job_id

    def _flush_pending_lines(self):
        if not self._pending_lines:
            return
        job_id = self._ensure_job_id()
        if not job_id:
            return
        pending = self._pending_lines
        self._pending_lines = []
        for line, level in pending:
            self._post_progress(job_id, text=line, level=level)

    def _load_env_file(self) -> dict[str, str]:
        settings: dict[str, str] = {}
        try:
            env_path = Path(__file__).with_name('dashboard.env')
            if not env_path.exists():
                return settings
            for raw_line in env_path.read_text(encoding='utf-8').splitlines():
                line = raw_line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' not in line:
                    continue
                key, value = line.split('=', 1)
                key = key.strip()
                if not key:
                    continue
                settings[key] = value.strip()
        except Exception:
            return {}
        return settings

    def _get_setting(self, name: str, default=None):
        value = os.getenv(name)
        if value is not None:
            return value
        try:
            if self._env_settings:
                fetched = self._env_settings.get(name)
                if fetched is not None:
                    return fetched
        except Exception:
            pass
        return default

    def _queue_message(self, text: str | None, level: str = 'info', split_lines: bool = False):
        if text is None:
            return
        try:
            rendered = str(text)
        except Exception:
            return
        if not rendered:
            return
        pieces = [rendered]
        if split_lines:
            pieces = rendered.splitlines(keepends=True) or [rendered]
        for piece in pieces:
            message = piece if piece.endswith('\n') else f"{piece}\n"
            self._pending_lines.append((message, level))
        self._flush_pending_lines()

    def _maybe_update_dashboard_url(self, play):
        context = self._collect_play_vars(play)
        try:
            url = context.get('dashboard_url') if context else None
            if url:
                self.dashboard_url = str(url).strip()
            job_name = context.get('dashboard_job_name') if context else None
            if job_name:
                self._job_name_override = str(job_name).strip()
            scope = context.get('dashboard_scope') if context else None
            if scope:
                self._scope_override = str(scope).strip()
            triggered = context.get('dashboard_triggered_by') if context else None
            if triggered:
                self._trigger_override = str(triggered).strip()
        except Exception:
            pass

    def _collect_play_vars(self, play):
        try:
            if play is None:
                return {}
            if hasattr(play, 'get_variable_manager') and callable(play.get_variable_manager):
                vm = play.get_variable_manager()
            else:
                vm = getattr(play, '_variable_manager', None)
            loader = getattr(play, '_loader', None)
            if vm and hasattr(vm, 'get_vars'):
                data = vm.get_vars(loader=loader, play=play)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {}

    def _ensure_job_started(self, play=None):
        if self._job_started:
            return
        context = self._collect_play_vars(play)
        job_name = self._job_name_override or context.get('dashboard_job_name') if context else None
        if not job_name:
            job_name = self._derive_job_name(play)
        scope = self._scope_override or context.get('dashboard_scope') if context else None
        if not scope:
            scope = self._derive_scope(play, context)
        triggered_by = self._trigger_override or (context.get('dashboard_triggered_by') if context else None)
        if not triggered_by:
            triggered_by = self._default_triggered_by()

        payload = {
            'job_name': job_name,
            'scope': scope,
            'triggered_by': triggered_by,
        }

        response = self._post_json(self._api_url('/api/jobs/start'), payload, expect_json=True)
        job_id = None
        if isinstance(response, dict):
            job_id = response.get('job_id')
        if job_id:
            self._update_job_id(job_id)
            self._job_started = True
            try:
                self._buffer.append(f"Dashboard job started (ID: {job_id})")
            except Exception:
                pass
            self._queue_message(f"Dashboard job started (ID: {job_id})", level='info')
        else:
            message = "Unable to register job with dashboard API."
            try:
                self._buffer.append(message)
            except Exception:
                pass
            self._queue_message(message, level='error')

    def _derive_job_name(self, play):
        if self._job_name_override:
            return self._job_name_override
        try:
            if play is not None:
                name = play.get_name().strip()
                if name:
                    return name
        except Exception:
            pass
        try:
            if play is not None and hasattr(play, '_filename') and play._filename:
                return Path(play._filename).stem
        except Exception:
            pass
        if self.playbook_dir:
            return Path(self.playbook_dir).name or 'Ansible Playbook'
        return 'Ansible Playbook'

    def _derive_scope(self, play, context):
        if self._scope_override:
            return self._scope_override
        limit = None
        try:
            if context and context.get('ansible_limit'):
                limit = str(context.get('ansible_limit')).strip()
        except Exception:
            limit = None
        if not limit:
            limit = os.getenv('ANSIBLE_LIMIT')
        if limit:
            limit = str(limit).strip()
            if ',' in limit:
                return f"servers:{limit}"
            return limit
        hosts = self._collect_hostnames(play, context)
        if hosts:
            unique_hosts = list(dict.fromkeys(hosts))
            if len(unique_hosts) == 1:
                return unique_hosts[0]
            return f"servers:{','.join(unique_hosts)}"
        return 'servers:unknown'

    def _collect_hostnames(self, play, context):
        hosts = []
        try:
            vm = None
            if play is not None:
                if hasattr(play, 'get_variable_manager') and callable(play.get_variable_manager):
                    vm = play.get_variable_manager()
                else:
                    vm = getattr(play, '_variable_manager', None)
            inventory = getattr(vm, '_inventory', None) if vm else None
            pattern = getattr(play, 'hosts', None) or 'all'
            if inventory and hasattr(inventory, 'get_hosts'):
                ansible_hosts = inventory.get_hosts(pattern)
                hosts = [h.get_name() for h in ansible_hosts if getattr(h, 'get_name', None)]
        except Exception:
            hosts = []
        if not hosts and context:
            try:
                play_hosts = context.get('ansible_play_hosts_all')
                if isinstance(play_hosts, (list, tuple)):
                    hosts = [str(h) for h in play_hosts]
                elif play_hosts:
                    hosts = [str(play_hosts)]
            except Exception:
                pass
        return [h for h in hosts if h]

    def _default_triggered_by(self):
        preset = self._get_setting('DASHBOARD_TRIGGERED_BY')
        if preset:
            return preset
        for env_var in ('USER', 'USERNAME'):
            value = os.getenv(env_var)
            if value:
                return value
        try:
            import getpass
            return getpass.getuser()
        except Exception:
            return 'ansible'

    def _accumulate_total_tasks(self, play):
        try:
            key = getattr(play, '_uuid', None) or id(play)
            if key in self._seen_play_uids:
                return
            self._seen_play_uids.add(key)
            total = self._count_play_tasks(play)
            if total:
                self._progress_total += total
        except Exception:
            pass

    def _count_play_tasks(self, play):
        try:
            compiled = play.compile() if hasattr(play, 'compile') else []
            total = 0
            for block in compiled:
                total += self._count_block_tasks(block)
            return total
        except Exception:
            return 0

    def _count_block_tasks(self, block):
        total = 0
        tasks = getattr(block, 'block', None)
        if not tasks and hasattr(block, 'get_tasks'):
            try:
                tasks = block.get_tasks()
            except Exception:
                tasks = None
        if tasks is None:
            return total
        for task in tasks:
            try:
                action = getattr(task, 'action', None)
                if action == 'meta':
                    continue
                total += 1
                if hasattr(task, 'block'):
                    total += self._count_block_tasks(task)
                for rescue in getattr(task, 'rescue', []) or []:
                    total += self._count_block_tasks(rescue)
                for always in getattr(task, 'always', []) or []:
                    total += self._count_block_tasks(always)
            except Exception:
                continue
        return total

    def _record_task_start(self, task):
        try:
            if getattr(task, 'action', None) == 'meta':
                return
        except Exception:
            pass
        self._tasks_started += 1
        if self._progress_total <= 0:
            return
        job_id = self._ensure_job_id()
        if not job_id:
            return
        try:
            pct = int((self._tasks_started / max(1, self._progress_total)) * 100)
        except Exception:
            pct = 0
        pct = max(0, min(99, pct))
        if pct > self._last_progress_sent:
            self._post_progress(job_id, progress=pct)
            self._last_progress_sent = pct

    def _format_failure_detail(self, result, prefix: str | None = None):
        try:
            task_name = getattr(result, '_task', None)
            if task_name and hasattr(task_name, 'get_name'):
                task_name = task_name.get_name()
            elif hasattr(task_name, 'name'):
                task_name = task_name.name
            if not task_name:
                task_name = 'Unknown task'
        except Exception:
            task_name = 'Unknown task'
        try:
            error_json = json.dumps(result._result, default=str)[:1000]
        except Exception:
            error_json = str(getattr(result, '_result', ''))
        header = prefix or 'Task failed'
        return f"{header}: {task_name}\nDetails: {error_json}"

    def _api_url(self, path: str) -> str:
        base = (self.dashboard_url or 'http://localhost:8000').rstrip('/')
        suffix = path if path.startswith('/') else f'/{path}'
        return f"{base}{suffix}"

    def _post_completion(self, job_id: int, status: str, message: str | None = None):
        payload = {
            'job_id': int(job_id),
            'status': status,
        }
        if message:
            payload['message'] = message
        self._post_json(self._api_url('/api/jobs/complete'), payload)

    def _short_result(self, result):
        try:
            data = result._result.copy()
            # keep it brief
            keys = [k for k in data.keys() if k in ("changed", "msg", "rc", "stdout", "stderr", "failed", "skipped")]
            out = {k: data.get(k) for k in keys}
            return json.dumps(out, default=str)[:500]
        except Exception:
            return "{}"

    def _delegate_suffix(self, result) -> str:
        try:
            dv = result._result.get('delegated_vars') or {}
            to = dv.get('delegated_host') or dv.get('delegate_to')
            if to:
                return f" -> {to}"
        except Exception:
            pass
        return ''



    def _emit(self, s: str):
        try:
            text = str(s)
        except Exception:
            return
        try:
            self._buffer.append(text)
        except Exception:
            pass
        self._queue_message(text, level='info', split_lines=True)

    def _get_custom_stat(self, stats, key: str):
        """Best-effort retrieval of custom stats across Ansible versions and shapes.
        - Newer ansible stores under stats.custom (sometimes nested under '_run').
        - Older may expose stats._custom or a get_custom_stats() accessor.
        This searches recursively for the first occurrence of 'key'.
        """
        def find_in(obj, target):
            try:
                if isinstance(obj, dict):
                    # direct hit
                    if target in obj:
                        return obj.get(target)
                    # special case: ansible stores under '_run': [ {host: {key: val}} , ... ]
                    if '_run' in obj:
                        return find_in(obj.get('_run'), target)
                    # otherwise, search nested dict values
                    for v in obj.values():
                        found = find_in(v, target)
                        if found is not None:
                            return found
                elif isinstance(obj, (list, tuple)):
                    for it in obj:
                        found = find_in(it, target)
                        if found is not None:
                            return found
            except Exception:
                return None
            return None

        # Try .custom
        try:
            custom = getattr(stats, 'custom', None)
            if custom is not None:
                val = find_in(custom, key)
                if val is not None:
                    return val
        except Exception:
            pass
        # Try ._custom
        try:
            custom = getattr(stats, '_custom', None)
            if custom is not None:
                val = find_in(custom, key)
                if val is not None:
                    return val
        except Exception:
            pass
        # Try accessor
        try:
            getter = getattr(stats, 'get_custom_stats', None)
            if callable(getter):
                custom = getter()
                val = find_in(custom, key)
                if val is not None:
                    return val
        except Exception:
            pass
        return None
