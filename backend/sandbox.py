"""Path confinement and bounded Docker execution; no host-code fallback."""
from pathlib import Path, PurePosixPath
import copy
import errno
import os
import re
import selectors
import shutil
import stat
import subprocess
import threading
import time
import uuid

from .agents import AGENTS

MAX_FILE_BYTES = 256 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
SAFE_TEXT_SUFFIXES = {'.md', '.txt', '.py', '.json', '.csv', '.tsv', '.log', '.yaml', '.yml', '.toml', '.ini', '.js', '.html', '.css', '.sql', '.xml'}
_ID = re.compile(r'^[A-Za-z0-9_-]{1,96}$')
_IMAGE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9./_:@-]{0,180}$')


class ToolError(Exception):
    def __init__(self, code, message, retryable=False):
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.retryable = bool(retryable)


def identifier(value, label='identifier'):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ToolError('parameter', f'{label} must be 1–96 letters, digits, underscores or hyphens.')
    return value


def relative_parts(value, allow_root=False):
    if not isinstance(value, str) or len(value) > 240 or not value:
        raise ToolError('parameter', 'path must be a nonempty relative string of at most 240 characters.')
    if allow_root and value in ('.', ''):
        return ()
    if value.startswith(('/', '~')) or '\\' in value or ':' in value or any(ord(c) < 32 for c in value):
        raise ToolError('permission', 'Absolute, device and control-character paths are forbidden.')
    pieces = value.split('/')
    if len(pieces) > 12 or any(not p or p in ('.', '..') or p.startswith('.') for p in pieces):
        raise ToolError('permission', 'Traversal, hidden files, .git and .env paths are forbidden.')
    if any(len(p) > 120 for p in pieces):
        raise ToolError('parameter', 'A path component is too long.')
    return tuple(pieces)


def secure_directory(base, *parts):
    """Create private directories, refusing symlinks in application-controlled descendants."""
    base = Path(base)
    if base.is_symlink():
        raise ToolError('permission', 'Data root must not be a symlink.')
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    current = base
    for part in parts:
        identifier(part, 'directory name')
        current = current / part
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ToolError('permission', 'A workspace directory is not a real directory.')
    return current


def confined_path(base, value, allow_root=False):
    parts = relative_parts(value, allow_root)
    base = Path(base)
    if base.is_symlink():
        raise ToolError('permission', 'Symlinked directory is forbidden.')
    result = base
    for part in parts:
        result = result / part
        if result.is_symlink():
            raise ToolError('permission', 'Symlinked paths are forbidden.')
    try:
        result.resolve(strict=False).relative_to(base.resolve())
    except ValueError as error:
        raise ToolError('permission', 'Path escapes the private workspace.') from error
    return result


def _parent_fd(base, value, create=False):
    """Open each parent via openat/O_NOFOLLOW to prevent symlink-swap races."""
    parts = relative_parts(value)
    flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    try:
        fd = os.open(base, flags)
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd, parts[-1]
    except OSError as error:
        if 'fd' in locals():
            os.close(fd)
        raise ToolError('permission' if error.errno in (errno.ELOOP, errno.ENOTDIR) else 'permanent', 'Cannot access a confined workspace path.') from error


def read_text(base, value, limit=MAX_FILE_BYTES):
    parent, name = _parent_fd(base, value)
    fd = None
    try:
        fd = os.open(name, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0), dir_fd=parent)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ToolError('permission', 'Only regular, non-linked files may be read.')
        if info.st_size > limit:
            raise ToolError('parameter', 'File exceeds the text-size limit.')
        content = bytearray()
        while len(content) <= limit:
            block = os.read(fd, min(65536, limit + 1 - len(content)))
            if not block:
                break
            content.extend(block)
        if len(content) > limit or b'\0' in content:
            raise ToolError('parameter', 'File is too large or contains binary data.')
        try:
            return bytes(content).decode('utf-8')
        except UnicodeDecodeError as error:
            raise ToolError('parameter', 'Only UTF-8 text is supported.') from error
    except OSError as error:
        raise ToolError('permission' if error.errno in (errno.ELOOP, errno.EACCES) else 'permanent', 'Cannot read the requested regular file.') from error
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def write_text(base, value, content, overwrite=False):
    if not isinstance(content, str) or len(content.encode('utf-8')) > MAX_FILE_BYTES or '\0' in content:
        raise ToolError('parameter', 'content must be UTF-8 text of at most 256 KiB.')
    parent, name = _parent_fd(base, value, create=True)
    fd = None
    try:
        # Open without truncation; verify inode/type before changing any existing file.
        flags = os.O_WRONLY | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
        if not overwrite:
            flags |= os.O_EXCL
        fd = os.open(name, flags, 0o600, dir_fd=parent)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ToolError('permission', 'Only regular, non-linked files may be modified.')
        if overwrite:
            os.ftruncate(fd, 0)
        data = content.encode('utf-8')
        while data:
            data = data[os.write(fd, data):]
        os.fsync(fd)
    except OSError as error:
        raise ToolError('permission' if error.errno in (errno.ELOOP, errno.EEXIST, errno.EACCES) else 'permanent', 'Cannot modify the confined file.') from error
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def delete_file(base, value):
    parent, name = _parent_fd(base, value)
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ToolError('permission', 'Only regular, non-linked files may be deleted.')
        os.unlink(name, dir_fd=parent)
    except OSError as error:
        raise ToolError('permanent', 'Cannot delete the requested regular file.') from error
    finally:
        os.close(parent)


def run_process(argv, *, cwd=None, env=None, timeout=20):
    """No shell, bounded stdout/stderr, and a deadline. Output overflow terminates the process."""
    start = time.monotonic()
    try:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)
    except OSError as error:
        raise ToolError('permanent', f'Cannot start executable: {Path(argv[0]).name}.') from error
    selector = selectors.DefaultSelector()
    buffers = {'stdout': bytearray(), 'stderr': bytearray()}
    for key, stream in (('stdout', process.stdout), ('stderr', process.stderr)):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, key)
    timed_out = False
    overflow = False
    try:
        while selector.get_map():
            remaining = timeout - (time.monotonic() - start)
            if remaining <= 0:
                timed_out = True
                break
            for selected, _ in selector.select(min(0.1, remaining)):
                block = os.read(selected.fileobj.fileno(), 65536)
                if not block:
                    selector.unregister(selected.fileobj)
                    continue
                target = buffers[selected.data]
                room = MAX_OUTPUT_BYTES - len(target)
                target.extend(block[:max(0, room)])
                if len(block) > room:
                    overflow = True
                    break
            if overflow:
                break
        if timed_out or overflow:
            process.kill()
        process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        process.kill()
        process.wait()
        raise ToolError('transient', 'Subprocess communication failed.', retryable=True)
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
    return {'exitCode': process.returncode, 'stdout': buffers['stdout'].decode('utf-8', 'replace'), 'stderr': buffers['stderr'].decode('utf-8', 'replace'), 'timedOut': timed_out, 'outputLimitExceeded': overflow, 'durationMs': round((time.monotonic() - start) * 1000)}


class SandboxManager:
    def __init__(self, config, agents=None):
        self.data_dir = Path(config.data_dir)
        self.agents = copy.deepcopy(AGENTS if agents is None else agents)
        if not isinstance(self.agents, dict) or not self.agents:
            raise ToolError('parameter', 'A nonempty agent registry is required.')
        for agent_id in self.agents:
            identifier(agent_id, 'agent_id')
        self.image = getattr(config, 'docker_image', 'python:3.11-alpine')
        if not isinstance(self.image, str) or not _IMAGE.fullmatch(self.image):
            raise ToolError('parameter', 'Invalid Docker image name.')
        self.docker = shutil.which('docker')
        self._containers = {}
        self._cancelled = set()
        self._lock = threading.RLock()

    def workspace(self, agent_id, run_id):
        if agent_id not in self.agents:
            raise ToolError('permission', 'Unknown agent.')
        identifier(run_id, 'run_id')
        return secure_directory(self.data_dir, 'workspaces', agent_id, run_id)

    def imports(self, agent_id, run_id):
        if agent_id not in self.agents:
            raise ToolError('permission', 'Unknown agent.')
        identifier(run_id, 'run_id')
        return secure_directory(self.data_dir, 'imports', agent_id, run_id)

    def _environment(self):
        # Docker CLI may use the user's Docker Desktop context; no model/API secrets are forwarded.
        return {key: value for key, value in os.environ.items() if key in ('PATH', 'HOME', 'DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_CONFIG', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH', 'LANG')}

    def capabilities(self):
        result = {'dockerInstalled': bool(self.docker), 'dockerAvailable': False, 'dockerImage': self.image, 'dockerImageReady': False, 'network': 'none', 'hostFallback': False}
        if not self.docker:
            result['reason'] = 'Docker CLI is not installed. Code execution requires Docker.'
            return result
        try:
            info = run_process([self.docker, 'info', '--format', '{{.ServerVersion}}'], env=self._environment(), timeout=5)
            result['dockerAvailable'] = info['exitCode'] == 0 and not info['timedOut']
            if result['dockerAvailable']:
                result['version'] = info['stdout'].strip()
                image = run_process([self.docker, 'image', 'inspect', '--format', '{{.Id}}', self.image], env=self._environment(), timeout=5)
                result['dockerImageReady'] = image['exitCode'] == 0 and not image['timedOut']
            if not result['dockerAvailable']:
                result['reason'] = 'Docker daemon is unavailable; start Docker Desktop/Engine.'
            elif not result['dockerImageReady']:
                result['reason'] = 'The configured image is not local. An operator must pull it explicitly.'
        except ToolError as error:
            result['reason'] = error.message
        return result

    def run(self, agent_id, run_id, args, test=False):
        workspace = self.workspace(agent_id, run_id)
        imports = self.imports(agent_id, run_id)
        with self._lock:
            if run_id in self._cancelled:
                raise ToolError('cancelled', 'This run was cancelled.')
        status = self.capabilities()
        if not status['dockerAvailable'] or not status['dockerImageReady']:
            raise ToolError('permanent', status.get('reason', 'Docker is unavailable.'))
        supplied_code = args.get('code')
        supplied_path = args.get('path')
        if bool(supplied_code is not None) == bool(supplied_path is not None):
            raise ToolError('parameter', 'Supply exactly one of code or path.')
        generated = None
        if supplied_code is not None:
            if not isinstance(supplied_code, str) or not supplied_code.strip() or len(supplied_code.encode('utf-8')) > 64 * 1024:
                raise ToolError('parameter', 'code must be nonempty Python source of at most 64 KiB.')
            generated = f'run_{uuid.uuid4().hex}.py'
            write_text(workspace, generated, supplied_code)
            script = '/workspace/' + generated
        else:
            relative_parts(supplied_path)
            if supplied_path.startswith('imports/'):
                imported = supplied_path[len('imports/'):]
                read_text(imports, imported)
                script = '/imports/' + imported
            else:
                read_text(workspace, supplied_path)
                script = '/workspace/' + supplied_path
        arguments = args.get('args', [])
        if not isinstance(arguments, list) or len(arguments) > 30 or any(not isinstance(item, str) or len(item) > 1024 or '\0' in item for item in arguments):
            raise ToolError('parameter', 'args must contain at most 30 short strings.')
        duration = args.get('timeout', 20)
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not 1 <= duration <= 60:
            raise ToolError('parameter', 'timeout must be between 1 and 60 seconds.')
        if any(',' in str(path) for path in (workspace, imports)):
            raise ToolError('parameter', 'Docker bind paths cannot contain commas.')
        name = 'evo-' + uuid.uuid4().hex
        command = [self.docker, 'run', '--rm', '--pull=never', '--name', name,
                   '--network=none', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                   '--memory=256m', '--memory-swap=256m', '--cpus=1', '--pids-limit=64',
                   '--ulimit', 'fsize=8388608:8388608', '--ulimit', 'nofile=128:128',
                   '--user', f'{os.getuid()}:{os.getgid()}',
                   '--tmpfs', '/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777',
                   '--mount', f'type=bind,src={workspace.resolve()},dst=/workspace',
                   '--mount', f'type=bind,src={imports.resolve()},dst=/imports,readonly',
                   '--workdir', '/workspace', '--env', 'PYTHONDONTWRITEBYTECODE=1', '--env', 'PYTHONUNBUFFERED=1',
                   '--entrypoint', 'python3', self.image, '-B', '-s', '-E', script, *arguments]
        with self._lock:
            if run_id in self._cancelled:
                if generated:
                    delete_file(workspace, generated)
                raise ToolError('cancelled', 'This run was cancelled before container creation.')
            self._containers[name] = run_id
        try:
            result = run_process(command, env=self._environment(), timeout=duration)
            result.update({'ok': result['exitCode'] == 0 and not result['timedOut'] and not result['outputLimitExceeded'], 'sandboxId': name, 'test': bool(test)})
            if result['timedOut']:
                result['error'] = 'Code execution timed out.'
            elif result['outputLimitExceeded']:
                result['error'] = 'Code execution exceeded the 64 KiB stdout/stderr limit.'
            with self._lock:
                if run_id in self._cancelled:
                    result.update({'ok': False, 'cancelled': True, 'error': 'Run cancelled.'})
            return result
        finally:
            # --rm handles normal exits; exact generated names prevent killing unrelated containers.
            try:
                run_process([self.docker, 'rm', '-f', name], env=self._environment(), timeout=5)
            except ToolError:
                pass
            with self._lock:
                self._containers.pop(name, None)
            if generated:
                try:
                    delete_file(workspace, generated)
                except ToolError:
                    pass

    def cancel(self, run_id):
        identifier(run_id, 'run_id')
        with self._lock:
            self._cancelled.add(run_id)
            targets = [name for name, owner_run in self._containers.items() if owner_run == run_id]
        for name in targets:
            if self.docker:
                try:
                    run_process([self.docker, 'rm', '-f', name], env=self._environment(), timeout=5)
                except ToolError:
                    pass
        return {'ok': True, 'cancelledContainers': len(targets)}
