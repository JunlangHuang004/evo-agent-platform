"""Optional stderr trace for the local server. Never writes the provider protocol stdout."""
import sys
import threading
import time

_lock = threading.Lock()


def emit(config, label, body=''):
    if not getattr(config, 'terminal_trace', False):
        return
    text = body if isinstance(body, str) else ''
    if text and hasattr(config, 'scrub'):
        text = config.scrub(text)
    stamp = time.strftime('%H:%M:%S')
    with _lock:
        sys.stderr.write(f'[{stamp}] {label}\n')
        if text:
            sys.stderr.write(text.rstrip() + '\n')
        sys.stderr.write('\n')
        sys.stderr.flush()
