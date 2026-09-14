"""Codex 0.154 usage observations; source and freshness: docs/SESSION-STATUS.md."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import stat
import tempfile
import time

logger = logging.getLogger(__name__)
TOKEN_READ_BYTES = 1024 * 1024
ACCOUNT_INTERVAL = 60.0
TOKEN_FIELDS = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
                'output_tokens', 'reasoning_output_tokens', 'total_tokens')


def stamp():
    return datetime.now(timezone.utc).isoformat()


def _token_fields(value):
    if not isinstance(value, dict):
        return None
    return {key: value[key] for key in TOKEN_FIELDS if key in value}


def token_snapshot(path, thread_id):
    """Read the latest complete counter in a bounded rollout tail, never its mtime."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        size = os.fstat(stream.fileno())
        if not stat.S_ISREG(size.st_mode):
            return None
        offset = max(0, size.st_size - TOKEN_READ_BYTES)
        stream.seek(offset)
        lines = stream.read(TOKEN_READ_BYTES).splitlines(keepends=True)
    if offset:
        lines = lines[1:]
    for line in reversed(lines):
        if not line.endswith(b'\n'):
            continue
        try:
            record = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(record, dict) or record.get('type') != 'event_msg':
            continue
        payload = record.get('payload')
        if not isinstance(payload, dict) or payload.get('type') != 'token_count':
            continue
        info = payload.get('info')
        if not isinstance(info, dict):
            continue
        return {
            'updated': record.get('timestamp'), 'source': 'rollout/token_count',
            'thread_id': thread_id, 'scope': 'thread',
            'last': _token_fields(info.get('last_token_usage')),
            'total': _token_fields(info.get('total_token_usage')),
            'context_window': info.get('model_context_window'),
        }
    return None


def account_snapshot(reply):
    """Preserve independent quota buckets; do not infer their model applicability."""
    if not isinstance(reply, dict):
        return None
    buckets = reply.get('rateLimitsByLimitId')
    if not isinstance(buckets, dict) or not buckets:
        single = reply.get('rateLimits')
        if not isinstance(single, dict):
            return None
        buckets = {single.get('limitId') or 'codex': single}
    limits = []
    for ident, bucket in list(buckets.items())[:8]:
        if not isinstance(bucket, dict):
            continue
        for key in ('primary', 'secondary'):
            window = bucket.get(key)
            if not isinstance(window, dict):
                continue
            limits.append({
                'key': f'{ident}:{key}', 'bucket': ident,
                'label': bucket.get('limitName') or ident,
                'percent': window.get('usedPercent'),
                'minutes': window.get('windowDurationMins'),
                'resets_at': window.get('resetsAt'),
            })
    return {'updated': stamp(), 'source': 'account/rateLimits/read',
            'scope': 'account', 'limits': limits[:8]}


class CodexUsage:
    def __init__(self):
        root = os.environ.get('LMER_STATUS_DIR')
        self.directory = Path(root) if root else None
        self.thread_id = None
        self.tokens = None
        self.account = None
        self.next_account = 0.0

    def refresh(self, thread, runtime, request):
        """Observe without changing native settings or disrupting input delivery."""
        if self.directory is None or not self.directory.is_dir():
            return
        if self.thread_id != thread['id']:
            self.thread_id = thread['id']
            self.tokens = self.account = None
            self.next_account = 0.0
        try:
            path = thread.get('path')
            if path:
                observed = token_snapshot(path, self.thread_id)
                if observed is not None:
                    self.tokens = observed
        except (OSError, ValueError, TypeError):
            logger.debug('Codex token observation unavailable', exc_info=True)
        if time.monotonic() >= self.next_account:
            self.next_account = time.monotonic() + ACCOUNT_INTERVAL
            try:
                observed = account_snapshot(request('account/rateLimits/read', {}))
                if observed is not None:
                    self.account = observed
            except Exception:
                logger.debug('Codex account observation unavailable', exc_info=True)
        document = {
            'schema': 1, 'harness': 'codex', 'updated': stamp(),
            'codex': {
                'version': thread.get('cliVersion'), 'thread_id': self.thread_id,
                'tokens': self.tokens, 'account': self.account,
                'runtime': {'updated': datetime.fromtimestamp(runtime['observed_at'], timezone.utc).isoformat(),
                            'source': 'thread/read', 'model': runtime.get('model'),
                            'effort': runtime.get('effort')},
            },
        }
        try:
            self._publish(document)
        except OSError:
            logger.debug('Codex status publishing unavailable', exc_info=True)

    def _publish(self, document):
        fd, name = tempfile.mkstemp(dir=self.directory, prefix='.codex-status-')
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(document, stream)
            os.replace(name, self.directory / 'status.json')
        finally:
            if os.path.exists(name):
                os.unlink(name)
