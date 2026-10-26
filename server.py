#!/usr/bin/env python3
"""Gitglass — a local, read-only Git review workspace. Python 3.10+, Git."""
import argparse
import json
import os
import subprocess
import threading
import time
import mimetypes
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE = Path(__file__).resolve().parent / 'static'
ROOTS = [Path.home() / 'Documents']
REPOS = {}
KNOWN = set()
LOCK = threading.Lock()
SKIP = {'.git', 'node_modules', '.cache', '.venv', 'venv', 'vendor', 'target', 'dist', '.local', '.trash'}

def git(repo, *args, allow_error=False):
    p = subprocess.run(['git', '-c', 'core.quotepath=false', '-c', 'color.ui=false', '-C', str(repo), *args],
                       capture_output=True, timeout=15, env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'})
    if p.returncode and not allow_error:
        raise ValueError(p.stderr.decode(errors='replace').strip() or 'Git command failed')
    return p.stdout.decode('utf-8', errors='replace')

def scan(root=None):
    roots = [Path(root).expanduser().resolve()] if root else ROOTS
    found = {}
    for base in roots:
        if not base.is_dir():
            raise ValueError('扫描目录不存在')
        # Bounded breadth and depth keep discovery responsive on large home directories.
        visited = 0
        for directory, dirs, files in os.walk(base, followlinks=False):
            visited += 1
            if visited > 15000:
                break
            current = Path(directory)
            dirs[:] = [d for d in dirs if d.lower() not in SKIP and not d.startswith('.')]
            if len(current.relative_to(base).parts) >= 6:
                dirs[:] = []
            if (current / '.git').exists():
                try:
                    status = git(current, 'status', '--porcelain=v1', '-z')
                    if status:
                        key = str(current.resolve())
                        found[key] = {'id': key, 'name': current.name, 'path': key,
                                      'branch': git(current, 'branch', '--show-current').strip() or 'detached HEAD'}
                except (ValueError, subprocess.TimeoutExpired):
                    pass
    with LOCK:
        if root:
            resolved = Path(root).expanduser().resolve()
            if resolved not in ROOTS:
                ROOTS.append(resolved)
            # Preserve other roots while removing stale entries inside this root.
            for key in list(REPOS):
                if Path(key).is_relative_to(resolved):
                    del REPOS[key]
        else:
            REPOS.clear()
        REPOS.update(found)
        KNOWN.update(found)
        return {'repos': list(REPOS.values()), 'roots': [str(r) for r in ROOTS]}

def changed_files(repo, commit=None):
    files = []
    if commit:
        raw = git(repo, 'diff-tree', '--root', '--no-commit-id', '--name-status', '--no-renames', '-r', '-z', commit).split('\0')
        i = 0
        while i + 1 < len(raw):
            status, name = raw[i:i+2]
            if status:
                files.append({'path': name, 'status': status[0]})
            i += 2
    else:
        entries = git(repo, 'status', '--porcelain=v1', '-z', '--untracked-files=all').split('\0')
        i = 0
        while i < len(entries):
            entry = entries[i]
            i += 1
            if not entry:
                continue
            status, name = entry[:2], entry[3:]
            files.append({'path': name, 'status': '?' if status == '??' else next((c for c in status if c != ' '), 'M')})
            if 'R' in status or 'C' in status:
                i += 1
    return files

def history(repo):
    raw = git(repo, 'log', '-60', '--format=%H%x00%h%x00%s%x00%an%x00%aI%x00', allow_error=True)
    parts = raw.split('\0')
    return [dict(zip(['id', 'short', 'message', 'author', 'date'], [parts[i].strip(), *parts[i+1:i+5]]))
            for i in range(0, len(parts)-5, 5)]

def diff(repo, name, commit=None):
    # Literal pathspecs and `--` keep names containing Git syntax inert.
    spec = ':(literal)' + name
    if commit:
        return git(repo, 'show', '--format=', '--no-ext-diff', '--no-textconv', '--no-renames', '--unified=5', commit, '--', spec)
    content = git(repo, 'diff', '--no-ext-diff', '--no-textconv', '--no-renames', '--unified=5', 'HEAD', '--', spec, allow_error=True)
    if not content:
        content = git(repo, 'diff', '--no-ext-diff', '--no-textconv', '--unified=5', '--', spec, allow_error=True)
        content += git(repo, 'diff', '--cached', '--no-ext-diff', '--no-textconv', '--unified=5', '--', spec, allow_error=True)
    if not content:
        target = (Path(repo) / name).resolve()
        if not target.is_relative_to(Path(repo).resolve()):
            raise ValueError('文件路径无效')
        if target.is_file():
            if target.stat().st_size > 1024 * 1024:
                return '文件大于 1 MB，请在编辑器中查看。'
            data = target.read_bytes()
            if b'\0' in data:
                return 'Binary file — 无法显示文本差异'
            lines = data.decode('utf-8', errors='replace').splitlines()
            content = '@@ -0,0 +1,%d @@\n' % len(lines) + '\n'.join('+' + line for line in lines)
    return content[:2_000_000]

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            args = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            if parsed.path.startswith('/api/'):
                if parsed.path == '/api/scan':
                    result = scan(args.get('root'))
                else:
                    repo = args.get('repo', '')
                    with LOCK:
                        valid = repo in KNOWN
                    if not valid:
                        raise ValueError('请先扫描并选择仓库')
                    commit = args.get('commit') or None
                    if commit and (len(commit) != 40 or any(c not in '0123456789abcdef' for c in commit)):
                        raise ValueError('提交 ID 无效')
                    if parsed.path == '/api/state':
                        result = {'files': changed_files(repo, commit), 'commits': history(repo)}
                    elif parsed.path == '/api/diff':
                        result = {'diff': diff(repo, args.get('file', ''), commit)}
                    else:
                        raise ValueError('未知接口')
                self.respond(json.dumps(result, ensure_ascii=False).encode(), 'application/json')
            else:
                target = (BASE / (parsed.path.lstrip('/') or 'index.html')).resolve()
                if not target.is_relative_to(BASE) or not target.is_file():
                    self.send_error(404)
                    return
                self.respond(target.read_bytes(), mimetypes.guess_type(target)[0] or 'application/octet-stream')
        except Exception as exc:
            self.respond(json.dumps({'error': str(exc)}, ensure_ascii=False).encode(), 'application/json', 400)

    def respond(self, body, kind, status=200):
        self.send_response(status)
        self.send_header('Content-Type', kind + '; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', action='append', help='扫描目录，可重复指定')
    parser.add_argument('--port', type=int, default=8765)
    options = parser.parse_args()
    if options.root:
        ROOTS = [Path(p).expanduser().resolve() for p in options.root]
    print(f'Gitglass → http://127.0.0.1:{options.port}', flush=True)
    ThreadingHTTPServer(('127.0.0.1', options.port), Handler).serve_forever()
