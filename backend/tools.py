"""Structured server-side Tool Gateway. Never executes a model-supplied shell command."""
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, urljoin, urlencode, parse_qs
import hashlib
import base64
import http.client
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import stat
import threading
import time

from .agents import AGENTS
from .sandbox import (ToolError, SandboxManager, MAX_FILE_BYTES, SAFE_TEXT_SUFFIXES,
                      identifier, relative_parts, confined_path, secure_directory,
                      read_text, write_text, delete_file, run_process)

MAX_WEB_BYTES = 512 * 1024
_SHA = re.compile(r'^[a-f0-9]{64}$')
_COMMIT = re.compile(r'^[a-f0-9]{40,64}$')


def _schema(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}


_PATH = {'type': 'string', 'maxLength': 240, 'description': 'Relative private path, or read-only imports/<artifact-id>/<name>. Hidden and parent paths are forbidden.'}
_TEXT = {'type': 'string', 'maxLength': MAX_FILE_BYTES}
_EXPECTED = {'type': 'string', 'description': 'SHA256 returned by the pending approval; must match current file content.', 'pattern': '^[a-f0-9]{64}$'}
_SCHEMAS = {
    'file.read': _schema({'path': _PATH}, ['path']),
    'file.list': _schema({'path': _PATH}),
    'file.write': _schema({'path': _PATH, 'content': _TEXT, 'expectedSha256': _EXPECTED}, ['path', 'content']),
    'file.patch': _schema({'path': _PATH, 'old': _TEXT, 'new': _TEXT, 'expectedSha256': _EXPECTED}, ['path', 'old', 'new']),
    'file.delete': _schema({'path': _PATH, 'expectedSha256': _EXPECTED}, ['path']),
    'web.search': _schema({'query': {'type': 'string', 'maxLength': 1000}, 'max_results': {'type': 'integer', 'minimum': 1, 'maximum': 8}}, ['query']),
    'web.fetch': _schema({'url': {'type': 'string', 'maxLength': 2048}}, ['url']),
    'code.run': _schema({'code': {'type': 'string', 'maxLength': 65536}, 'path': _PATH, 'args': {'type': 'array', 'items': {'type': 'string', 'maxLength': 1024}, 'maxItems': 30}, 'timeout': {'type': 'number', 'minimum': 1, 'maximum': 60}}),
    'code.test': _schema({'code': {'type': 'string', 'maxLength': 65536}, 'path': _PATH, 'args': {'type': 'array', 'items': {'type': 'string', 'maxLength': 1024}, 'maxItems': 30}, 'timeout': {'type': 'number', 'minimum': 1, 'maximum': 60}}),
    'git.status': _schema({}),
    'git.commit': _schema({'message': {'type': 'string', 'maxLength': 500}, 'files': {'type': 'array', 'items': _PATH, 'maxItems': 40}}, ['message']),
    'git.push': _schema({'remote': {'type': 'string', 'maxLength': 2048}, 'branch': {'type': 'string', 'maxLength': 240}, 'commit': {'type': 'string', 'pattern': '^[a-f0-9]{40,64}$'}}, ['remote', 'branch', 'commit']),
}
_DESCRIPTIONS = {
    'file.read': 'Read bounded UTF-8 text in this agent/run workspace or explicitly granted imports.',
    'file.list': 'List public files in this private workspace or read-only imports.',
    'file.write': 'Create UTF-8 text. Existing-file replacement requires human approval bound to its SHA256; prefer patch.',
    'file.patch': 'Apply one unique exact old→new text replacement; requires human approval and unchanged original SHA256.',
    'file.delete': 'Delete one regular private file after human approval bound to its SHA256.',
    'web.search': 'Search public web sources through DuckDuckGo Lite; returns actual links, never fabricated search results.',
    'web.fetch': 'Fetch bounded public HTTP(S) text with DNS/IP pinning and per-redirect SSRF checks.',
    'code.run': 'Run Python source or a relative Python script exclusively in a resource-limited Docker container.',
    'code.test': 'Run a Python verification/test script exclusively in this agent’s isolated Docker container.',
    'git.status': 'Inspect this run’s isolated Git repository and return a pushTarget for the fixed configured remote.',
    'git.commit': 'Create a real local commit of specified safe text files; Git metadata and hooks are isolated from code.',
    'git.push': 'Push the exact approved commit to the fixed HTTPS remote and per-agent/per-run branch; no force or shell.',
}


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []
        self.title = []
        self.ignore = 0
        self.in_title = False
        self.links = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ('script', 'style', 'noscript', 'svg'):
            self.ignore += 1
        if tag == 'title':
            self.in_title = True
        if tag == 'a':
            self.current = {'url': attrs.get('href', ''), 'title': '', 'class': attrs.get('class', '')}
        if tag in ('p', 'div', 'br', 'li', 'h1', 'h2', 'h3', 'tr'):
            self.blocks.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript', 'svg') and self.ignore:
            self.ignore -= 1
        if tag == 'title':
            self.in_title = False
        if tag == 'a' and self.current is not None:
            self.links.append(self.current)
            self.current = None

    def handle_data(self, value):
        if self.ignore:
            return
        self.blocks.append(value)
        if self.in_title:
            self.title.append(value)
        if self.current is not None:
            self.current['title'] += value


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, address, port, timeout, tls=False):
        super().__init__(host, port=port, timeout=timeout)
        self.address = address
        self.tls = tls

    def connect(self):
        sock = socket.create_connection((self.address, self.port), timeout=self.timeout)
        try:
            actual = ipaddress.ip_address(sock.getpeername()[0])
            if not _public_address(actual):
                raise ToolError('permission', 'The connected address is not public.')
            if self.tls:
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)
            self.sock = sock
        except BaseException:
            sock.close()
            raise


def _public_address(address):
    if address.version == 6 and address.ipv4_mapped:
        return _public_address(address.ipv4_mapped)
    return address.is_global and not (address.is_multicast or address.is_reserved or address.is_loopback or address.is_link_local)


def _validate_url(url):
    """Resolve once and return validated IPs; connections pin one IP, avoiding DNS rebinding."""
    if not isinstance(url, str) or not 1 <= len(url) <= 2048 or '\\' in url or any(ord(c) < 33 for c in url):
        raise ToolError('parameter', 'URL must be a short public HTTP(S) URL without whitespace or backslashes.')
    try:
        parts = urlsplit(url)
        host = (parts.hostname or '').rstrip('.').encode('idna').decode('ascii').lower()
        port = parts.port or (443 if parts.scheme == 'https' else 80)
    except (ValueError, UnicodeError) as error:
        raise ToolError('parameter', 'Invalid URL.') from error
    if parts.scheme not in ('http', 'https') or parts.username is not None or parts.password is not None or not host:
        raise ToolError('permission', 'Only credential-free public HTTP(S) URLs are allowed.')
    if port != (443 if parts.scheme == 'https' else 80):
        raise ToolError('permission', 'Only the standard HTTP/HTTPS ports are allowed.')
    if '%' in host or host == 'localhost' or host.endswith(('.localhost', '.local', '.internal', '.home', '.lan')) or host in ('metadata.google.internal', 'metadata.google', 'instance-data'):
        raise ToolError('permission', 'Local and metadata hosts are forbidden.')
    try:
        direct = ipaddress.ip_address(host)
    except ValueError:
        direct = None
        if '.' not in host or not re.fullmatch(r'[a-z0-9.-]+', host):
            raise ToolError('permission', 'The host must be a public DNS name.')
    if direct is not None:
        addresses = [str(direct)]
    else:
        try:
            addresses = list(dict.fromkeys(info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
        except socket.gaierror as error:
            raise ToolError('transient', 'DNS lookup failed.', retryable=True) from error
    if not addresses or any(not _public_address(ipaddress.ip_address(address)) for address in addresses):
        raise ToolError('permission', 'Private, loopback, metadata, multicast and mixed public/private DNS targets are forbidden.')
    authority = f'[{host}]' if ':' in host else host
    normalized = urlunsplit((parts.scheme, authority, parts.path or '/', parts.query, ''))
    return normalized, host, port, addresses


class ToolGateway:
    def __init__(self, config):
        self.data_dir = Path(config.data_dir)
        self.sandbox = SandboxManager(config)
        self.git_remote = getattr(config, 'git_remote', '')
        self._git_token = getattr(config, 'git_token', '')
        self._git_username = getattr(config, 'git_username', 'x-access-token')
        if not isinstance(self._git_token, str) or len(self._git_token) > 4096 or any(ord(char) < 33 or ord(char) == 127 for char in self._git_token):
            raise ToolError('parameter', 'GIT_TOKEN must be a bounded token without whitespace or control characters.')
        if not isinstance(self._git_username, str) or not re.fullmatch(r'[A-Za-z0-9_.@-]{1,80}', self._git_username):
            raise ToolError('parameter', 'GIT_USERNAME must be a short credential username without colons or control characters.')
        self._git_basic = base64.b64encode((self._git_username + ':' + self._git_token).encode('utf-8')).decode('ascii') if self._git_token else ''
        self.timeout = max(1, min(60, getattr(config, 'tool_timeout', 20)))
        self.git = shutil.which('git')
        self._locks = {}
        self._lock = threading.RLock()
        if self.git_remote:
            self._check_remote_format(self.git_remote)

    def workspace(self, agent_id, run_id):
        return self.sandbox.workspace(agent_id, run_id)

    def capabilities(self):
        return {**self.sandbox.capabilities(), 'gitInstalled': bool(self.git), 'gitRemoteConfigured': bool(self.git_remote), 'gitTokenConfigured': bool(self._git_token), 'fileSizeLimit': MAX_FILE_BYTES, 'webByteLimit': MAX_WEB_BYTES}

    def definitions(self, agent_id):
        if agent_id not in AGENTS:
            raise ToolError('permission', 'Unknown agent.')
        return [
            {'name': name, 'description': _DESCRIPTIONS[name], 'parameters': _SCHEMAS[name],
             'risk': 'high' if name in ('git.push', 'file.delete') else 'medium' if name in ('file.write', 'file.patch', 'code.run', 'code.test', 'git.commit') else 'low',
             'permission': 'human_approval' if name in ('git.push', 'file.delete', 'file.patch') else 'approval_on_overwrite' if name == 'file.write' else 'agent_allowlist',
             'idempotent': name in ('file.read', 'file.list', 'file.write', 'web.search', 'web.fetch', 'git.status'), 'timeout': 60 if name.startswith('code.') else self.timeout}
            for name in AGENTS[agent_id]['tools']
        ]

    def cancel(self, run_id):
        return self.sandbox.cancel(run_id)

    def _redact(self, value):
        if isinstance(value, str):
            if self._git_token:
                variants = {self._git_token, self._git_basic, base64.b64encode(self._git_token.encode('utf-8')).decode('ascii')}
                for secret in sorted(variants, key=len, reverse=True):
                    value = value.replace(secret, '[REDACTED]')
            return value
        if isinstance(value, dict):
            return {key: self._redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        return value

    def _validate_args(self, name, args):
        if not isinstance(args, dict) or not all(isinstance(key, str) for key in args):
            raise ToolError('parameter', 'Tool arguments must be a JSON object.')
        schema = _SCHEMAS[name]
        if set(args) - set(schema['properties']) or any(key not in args for key in schema['required']):
            raise ToolError('parameter', f'Invalid or missing parameters for {name}.')
        for key, value in args.items():
            rule = schema['properties'][key]
            kind = rule['type']
            if kind == 'string':
                if not isinstance(value, str) or '\0' in value or len(value) > rule.get('maxLength', MAX_FILE_BYTES):
                    raise ToolError('parameter', f'{key} must be a bounded string.')
                if rule.get('pattern') and not re.fullmatch(rule['pattern'], value):
                    raise ToolError('parameter', f'{key} has an invalid format.')
            elif kind in ('number', 'integer'):
                if isinstance(value, bool) or not isinstance(value, (int, float)) or kind == 'integer' and not isinstance(value, int) or not rule.get('minimum', 0) <= value <= rule.get('maximum', 60):
                    raise ToolError('parameter', f'{key} is out of range.')
            elif kind == 'array':
                if not isinstance(value, list) or len(value) > rule.get('maxItems', 40) or any(not isinstance(item, str) or '\0' in item or len(item) > rule.get('items', {}).get('maxLength', 1024) for item in value):
                    raise ToolError('parameter', f'{key} must be a bounded string array.')

    def execute(self, agent_id, run_id, name, args, approved=False):
        if agent_id not in AGENTS or name not in AGENTS[agent_id]['tools'] or name not in _SCHEMAS:
            raise ToolError('permission', 'This agent is not allowed to use that tool.')
        identifier(run_id, 'run_id')
        self._validate_args(name, args)
        workspace = self.workspace(agent_id, run_id)
        with self._lock:
            lock = self._locks.setdefault((agent_id, run_id), threading.RLock())
        with lock:
            try:
                if name.startswith('file.'):
                    return self._redact(self._file(agent_id, run_id, name, args, bool(approved)))
                if name.startswith('web.'):
                    return self._redact(self._search(args) if name == 'web.search' else self._web_fetch(args['url']))
                if name.startswith('code.'):
                    return self._redact(self.sandbox.run(agent_id, run_id, args, test=name == 'code.test'))
                return self._redact(self._git(agent_id, run_id, name, args, bool(approved)))
            except ToolError as error:
                raise ToolError(error.code, self._redact(error.message), error.retryable) from None
            except (OSError, ValueError) as error:
                raise ToolError('permanent', 'The tool could not complete its bounded operation.') from None

    def _location(self, agent_id, run_id, path, writable=False, allow_root=False):
        if path in ('imports', 'imports/') and allow_root:
            if writable:
                raise ToolError('permission', 'Artifact imports are read-only.')
            return self.sandbox.imports(agent_id, run_id), '.'
        if isinstance(path, str) and path.startswith('imports/'):
            if writable:
                raise ToolError('permission', 'Artifact imports are read-only.')
            base, relative = self.sandbox.imports(agent_id, run_id), path[len('imports/'):]
        else:
            base, relative = self.workspace(agent_id, run_id), path
        confined_path(base, relative, allow_root)
        return base, relative

    def _approval(self, name, args, title, description):
        return {'ok': False, 'needsApproval': True, 'tool': name, 'args': dict(args), 'approval': {'title': title, 'description': description}}

    def _file(self, agent_id, run_id, name, args, approved):
        value = args.get('path', '.')
        base, relative = self._location(agent_id, run_id, value, writable=name not in ('file.read', 'file.list'), allow_root=name == 'file.list')
        path = confined_path(base, relative, allow_root=name == 'file.list')
        if name == 'file.read':
            content = read_text(base, relative)
            return {'ok': True, 'path': value, 'content': content, 'sha256': hashlib.sha256(content.encode()).hexdigest()}
        if name == 'file.list':
            if not path.is_dir():
                raise ToolError('parameter', 'The requested path is not a directory.')
            entries = []
            for child in sorted(path.iterdir()):
                if child.name.startswith('.') or child.is_symlink():
                    continue
                info = child.lstat()
                if stat.S_ISREG(info.st_mode) and info.st_nlink != 1 or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                    continue
                entries.append({'name': child.name, 'type': 'directory' if child.is_dir() else 'file', 'size': info.st_size})
                if len(entries) >= 100:
                    break
            return {'ok': True, 'path': value, 'entries': entries}
        exists = path.exists()
        old = read_text(base, relative) if exists else None
        digest = hashlib.sha256(old.encode()).hexdigest() if old is not None else None
        expected = args.get('expectedSha256')
        if expected is not None and expected != digest:
            raise ToolError('permission', 'The file changed since it was inspected/approved; read it again and request a new approval.')
        if name in ('file.patch', 'file.delete') and not exists:
            raise ToolError('permanent', 'The file does not exist.')
        replacement = args.get('content')
        if name == 'file.patch':
            before, after = args['old'], args['new']
            if not before or old.count(before) != 1:
                raise ToolError('parameter', 'Patch old text must occur exactly once; read the file and use a unique exact match.')
            replacement = old.replace(before, after, 1)
        if replacement is not None and (len(replacement.encode('utf-8')) > MAX_FILE_BYTES or '\0' in replacement):
            raise ToolError('parameter', 'Resulting text exceeds 256 KiB or contains binary data.')
        if exists:
            if not approved:
                bound = {**args, 'expectedSha256': digest}
                return self._approval(name, bound, f'Approve {name} for {value}?', f'Only this {agent_id}/{run_id} file; existing SHA256 {digest}. Replacement/deletion will be rejected if the file changes.')
            if expected != digest:
                raise ToolError('permission', 'Approval must include the exact expectedSha256 returned by the pending request.')
        elif approved and expected is not None:
            raise ToolError('permission', 'An approved existing file disappeared; request approval again.')
        if name == 'file.delete':
            delete_file(base, relative)
            return {'ok': True, 'path': value, 'deleted': True}
        write_text(base, relative, replacement, overwrite=exists)
        return {'ok': True, 'path': value, 'sha256': hashlib.sha256(replacement.encode()).hexdigest(), 'bytes': len(replacement.encode()), 'created': not exists}

    @staticmethod
    def _validate_url(url):
        return _validate_url(url)

    def _fetch_url(self, url):
        deadline = time.monotonic() + self.timeout
        current = url
        for redirect in range(6):
            normalized, host, port, addresses = _validate_url(current)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ToolError('transient', 'Web request timed out.', retryable=True)
            parsed = urlsplit(normalized)
            connection = _PinnedHTTP(host, addresses[0], port, min(remaining, self.timeout), tls=parsed.scheme == 'https')
            try:
                connection.request('GET', urlunsplit(('', '', parsed.path, parsed.query, '')), headers={'User-Agent': 'EvoAgentCourse/1.0', 'Accept': 'text/html,text/plain,application/json;q=0.9', 'Accept-Encoding': 'identity', 'Connection': 'close'})
                response = connection.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader('Location')
                    if not location or redirect == 5:
                        raise ToolError('permanent', 'Too many or invalid redirects.')
                    current = urljoin(normalized, location)
                    continue
                if response.status != 200:
                    retryable = response.status in (408, 429, 500, 502, 503, 504)
                    raise ToolError('transient' if retryable else 'permanent', f'Public web server returned HTTP {response.status}.', retryable)
                encoding = response.getheader('Content-Encoding', 'identity').lower()
                if encoding not in ('', 'identity'):
                    raise ToolError('permanent', 'Compressed web responses are rejected to bound decompression size.')
                mime = response.getheader('Content-Type', '').split(';')[0].strip().lower()
                if not (mime.startswith('text/') or mime in ('application/json', 'application/xml', 'application/xhtml+xml')):
                    raise ToolError('parameter', 'Only public text/HTML/JSON/XML resources are supported.')
                chunks = bytearray()
                while len(chunks) <= MAX_WEB_BYTES:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ToolError('transient', 'Web request exceeded its total deadline.', True)
                    if connection.sock:
                        connection.sock.settimeout(min(remaining, self.timeout))
                    block = response.read1(min(16384, MAX_WEB_BYTES + 1 - len(chunks)))
                    if not block:
                        break
                    chunks.extend(block)
                truncated = len(chunks) > MAX_WEB_BYTES
                raw = bytes(chunks[:MAX_WEB_BYTES])
                charset = response.headers.get_content_charset() or 'utf-8'
                try:
                    text = raw.decode(charset, 'replace')
                except LookupError:
                    text = raw.decode('utf-8', 'replace')
                return {'url': normalized, 'contentType': mime, 'content': text, 'truncated': truncated}
            except ToolError:
                raise
            except (OSError, http.client.HTTPException) as error:
                raise ToolError('transient', 'Public web request failed or timed out.', retryable=True) from error
            finally:
                connection.close()
        raise ToolError('permanent', 'Redirect limit exceeded.')

    def _web_fetch(self, url):
        response = self._fetch_url(url)
        content = response['content']
        title = response['url']
        if 'html' in response['contentType']:
            parser = _TextParser()
            parser.feed(content)
            title = ''.join(parser.title).strip()[:300] or title
            content = re.sub(r'[ \t\r\f\v]+', ' ', ''.join(parser.blocks))
            content = re.sub(r'\n\s*\n+', '\n\n', content).strip()
        return {'ok': True, 'url': response['url'], 'title': title, 'content': content[:120000], 'truncated': response['truncated'] or len(content) > 120000, 'sources': [{'title': title, 'url': response['url']}]}

    def _search(self, args):
        query = args['query'].strip()
        if not query:
            raise ToolError('parameter', 'Search query must not be empty.')
        url = 'https://lite.duckduckgo.com/lite/?' + urlencode({'q': query})
        response = self._fetch_url(url)
        parser = _TextParser()
        parser.feed(response['content'])
        results = []
        seen = set()
        for link in parser.links:
            if 'result-link' not in link['class']:
                continue
            target = urljoin(response['url'], link['url'])
            parsed = urlsplit(target)
            if parsed.hostname and parsed.hostname.endswith('duckduckgo.com'):
                target = parse_qs(parsed.query).get('uddg', [target])[0]
            parsed = urlsplit(target)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or target in seen:
                continue
            seen.add(target)
            results.append({'title': re.sub(r'\s+', ' ', link['title']).strip()[:300], 'url': target})
            if len(results) >= args.get('max_results', 5):
                break
        if not results:
            raise ToolError('transient', 'Public search returned no parseable results (it may require a CAPTCHA or rate-limit requests).', retryable=True)
        return {'ok': True, 'query': query, 'results': results, 'sources': results, 'provider': 'DuckDuckGo Lite', 'sourceUrl': response['url']}

    @staticmethod
    def _check_remote_format(remote):
        if not isinstance(remote, str) or not 1 <= len(remote) <= 2048 or any(ord(c) < 33 for c in remote) or '\\' in remote:
            raise ToolError('parameter', 'GIT_REMOTE must be a fixed credential-free HTTPS URL.')
        try:
            parsed = urlsplit(remote)
            valid = parsed.scheme == 'https' and parsed.hostname and parsed.username is None and parsed.password is None and not parsed.query and not parsed.fragment and parsed.port in (None, 443)
        except ValueError:
            valid = False
        if not valid:
            raise ToolError('permission', 'Local, SSH, credential-bearing and non-HTTPS Git remotes are forbidden.')

    def _git_env(self):
        home = secure_directory(self.data_dir, 'git-home')
        return {'PATH': os.environ.get('PATH', os.defpath), 'HOME': str(home), 'LANG': 'C.UTF-8', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
                'GIT_TERMINAL_PROMPT': '0', 'GIT_AUTHOR_NAME': 'Evo Agent', 'GIT_AUTHOR_EMAIL': 'agent@localhost.invalid', 'GIT_COMMITTER_NAME': 'Evo Agent', 'GIT_COMMITTER_EMAIL': 'agent@localhost.invalid'}

    def _git_base(self, agent_id, run_id):
        if not self.git:
            raise ToolError('permanent', 'Git is not installed.')
        metadata = secure_directory(self.data_dir, 'gitmeta', agent_id, run_id)
        workspace = self.workspace(agent_id, run_id)
        if not (metadata / 'HEAD').is_file():
            initialized = run_process([self.git, 'init', '--bare', '--template=', str(metadata)], env=self._git_env(), timeout=self.timeout)
            if initialized['exitCode']:
                raise ToolError('permanent', 'Cannot initialize isolated Git metadata.')
        options = [self.git, '--git-dir=' + str(metadata.resolve()), '--work-tree=' + str(workspace.resolve()),
                   '-c', 'core.bare=false', '-c', 'core.hooksPath=' + os.devnull, '-c', 'core.fsmonitor=false', '-c', 'core.pager=cat',
                   '-c', 'credential.helper=', '-c', 'protocol.allow=never', '-c', 'protocol.https.allow=always', '-c', 'http.followRedirects=false',
                   '-c', 'submodule.recurse=false', '-c', 'commit.gpgsign=false', '-c', 'gc.auto=0']
        branch = f'agent-runs/{agent_id}/{run_id}'
        head = run_process([*options, 'symbolic-ref', 'HEAD', 'refs/heads/' + branch], cwd=workspace, env=self._git_env(), timeout=self.timeout)
        if head['exitCode']:
            raise ToolError('permanent', 'Cannot select the fixed per-run branch.')
        return options, workspace, branch

    def _git_command(self, base, workspace, arguments, push_env=None):
        environment = self._git_env()
        if push_env is not None:
            environment.update(push_env)
        result = self._redact(run_process([*base, *arguments], cwd=workspace, env=environment, timeout=self.timeout))
        if result['timedOut'] or result['outputLimitExceeded']:
            raise ToolError('transient', 'Git timed out or exceeded its output limit.', retryable=True)
        return result

    def _head(self, base, workspace):
        head = self._git_command(base, workspace, ['rev-parse', '--verify', 'HEAD'])
        return head['stdout'].strip() if head['exitCode'] == 0 else None

    def _safe_commit_files(self, workspace, requested):
        if requested is None:
            requested = []
            for directory, folders, files in os.walk(workspace, followlinks=False):
                folders[:] = [item for item in folders if not item.startswith('.') and not (Path(directory) / item).is_symlink() and item != 'imports']
                for name in files:
                    if name.startswith('.') or Path(name).suffix.lower() not in SAFE_TEXT_SUFFIXES:
                        continue
                    requested.append((Path(directory) / name).relative_to(workspace).as_posix())
                    if len(requested) > 40:
                        raise ToolError('parameter', 'Select at most 40 explicit text files to commit.')
        if not requested:
            raise ToolError('parameter', 'There are no safe text files to commit.')
        safe = []
        for value in requested:
            relative_parts(value)
            if value.startswith('imports/') or Path(value).suffix.lower() not in SAFE_TEXT_SUFFIXES:
                raise ToolError('permission', 'Only private public-name text files may be committed.')
            read_text(workspace, value)
            if value not in safe:
                safe.append(value)
        return safe

    def _git(self, agent_id, run_id, name, args, approved):
        base, workspace, branch = self._git_base(agent_id, run_id)
        if name == 'git.status':
            result = self._git_command(base, workspace, ['status', '--short', '--untracked-files=normal', '--ignore-submodules=all'])
            result.update({'ok': result['exitCode'] == 0, 'pushTarget': {'remote': self.git_remote, 'branch': branch, 'commit': self._head(base, workspace)}})
            return result
        if name == 'git.commit':
            message = args['message'].strip()
            if not message or '\n' in message or '\r' in message:
                raise ToolError('parameter', 'Commit message must be one nonempty line.')
            files = self._safe_commit_files(workspace, args.get('files'))
            added = self._git_command(base, workspace, ['add', '--', *files])
            if added['exitCode']:
                raise ToolError('permanent', 'Git could not stage the selected safe files.')
            result = self._git_command(base, workspace, ['commit', '--no-gpg-sign', '-m', message, '--only', '--', *files])
            result.update({'ok': result['exitCode'] == 0, 'files': files, 'pushTarget': {'remote': self.git_remote, 'branch': branch, 'commit': self._head(base, workspace)}})
            return result
        if not self.git_remote:
            raise ToolError('permission', 'GIT_REMOTE is not configured; no push target is authorized.')
        self._check_remote_format(self.git_remote)
        expected = {'remote': self.git_remote, 'branch': branch, 'commit': self._head(base, workspace)}
        if not expected['commit'] or any(args[key] != value for key, value in expected.items()):
            raise ToolError('permission', 'Push target must exactly match the configured remote, fixed branch and current commit returned by git.status/commit.')
        _validate_url(self.git_remote)
        if not approved:
            return self._approval(name, expected, 'Approve the exact Git push?', f"Remote: {expected['remote']}\nBranch: {branch}\nCommit: {expected['commit']}\nNo force push or other target is allowed.")
        if not self._git_token:
            raise ToolError('permission', 'GIT_TOKEN is not configured; authenticated HTTPS push is unavailable. No global credential helper is used.')
        normalized, host, port, addresses = _validate_url(self.git_remote)
        address = addresses[0]
        pinned = f'{host}:{port}:[{address}]' if ':' in address else f'{host}:{port}:{address}'
        origin = urlsplit(normalized).scheme + '://' + urlsplit(normalized).netloc
        # GitHub's official checkout helper uses Basic base64(username:token). Scope the
        # header to the fixed remote origin and supply it only through this subprocess env.
        # Redirects remain disabled; no header is written to Git config or placed in argv.
        push_env = {'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': f'http.{origin}/.extraheader',
                    'GIT_CONFIG_VALUE_0': 'AUTHORIZATION: basic ' + self._git_basic}
        result = self._git_command([*base, '-c', 'http.curloptResolve=' + pinned], workspace,
            ['push', '--porcelain', '--no-verify', '--', self.git_remote, f"{expected['commit']}:refs/heads/{branch}"], push_env=push_env)
        result.update({'ok': result['exitCode'] == 0, 'pushTarget': expected})
        return result
