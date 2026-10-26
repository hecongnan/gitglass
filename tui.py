#!/usr/bin/env python3
"""Gitglass: a dependency-free, read-only, three-pane Git terminal viewer."""
import argparse
import concurrent.futures
import curses
import locale
import os
import re
import shutil
import sys
import time
import unicodedata
from pathlib import Path

import server as backend


def clean(text):
    return ''.join(c if c >= ' ' and c != '\x7f' else ' ' for c in str(text))


def width(text):
    return sum(2 if unicodedata.east_asian_width(c) in 'WF' else 0 if unicodedata.combining(c) else 1 for c in text)


def clip(text, size, offset=0):
    out, pos = '', 0
    for c in clean(text):
        n = width(c)
        if pos >= offset and pos + n <= offset + size:
            out += c
        pos += n
        if pos > offset + size:
            break
    return out


def diff_lines(raw):
    """Return rows with independent old/new line numbers and additions/deletions."""
    rows, old, new = [], 0, 0
    for line in raw.expandtabs(4).splitlines():
        if line.startswith(('diff ', 'index ', '--- ', '+++ ', 'new file ', 'deleted file ')):
            continue
        match = re.match(r'^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@', line)
        if match:
            old, new = map(int, match.groups())
            rows.append(('hunk', '', '', line))
        elif line.startswith('+'):
            rows.append(('add', '', new, line[1:])); new += 1
        elif line.startswith('-'):
            rows.append(('del', old, '', line[1:])); old += 1
        elif line.startswith(' '):
            rows.append(('text', old, new, line[1:])); old += 1; new += 1
        else:
            rows.append(('hunk', '', '', line))
    return rows


def paired_rows(rows):
    result, i = [], 0
    while i < len(rows):
        row = rows[i]
        if row[0] not in ('add', 'del'):
            result.append((row, row)); i += 1; continue
        removed, added = [], []
        while i < len(rows) and rows[i][0] in ('add', 'del'):
            (added if rows[i][0] == 'add' else removed).append(rows[i]); i += 1
        result.extend((removed[n] if n < len(removed) else None,
                       added[n] if n < len(added) else None)
                      for n in range(max(len(removed), len(added))))
    return result


def file_tree(files, collapsed, query=''):
    tree = {}
    for file in files:
        if query.lower() not in file['path'].lower():
            continue
        node = tree
        parts = file['path'].split('/')
        for i, part in enumerate(parts):
            if i == len(parts) - 1:
                node[part] = file
            else:
                node = node.setdefault(part, {})
    rows = []
    def walk(node, prefix='', depth=0):
        for name, entry in sorted(node.items(), key=lambda item: ('path' in item[1], item[0].lower())):
            path = prefix + name
            is_file = 'path' in entry and isinstance(entry.get('path'), str)
            rows.append({'path': path, 'name': name, 'depth': depth, 'folder': not is_file,
                         'status': entry.get('status', '') if is_file else ''})
            if not is_file and (path not in collapsed or query):
                walk(entry, path + '/', depth + 1)
    walk(tree)
    return rows


class App:
    def __init__(self, screen, roots, interval=30, theme='light'):
        self.screen, self.roots, self.interval = screen, roots, interval
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.pending = None
        self.after = None
        self.repo = None
        self.repos, self.files, self.commits, self.rows = [], [], [], []
        self.commit = None
        self.selected = ''
        self.file_index, self.history_index = 0, 0
        self.scroll, self.horizontal = 0, 0
        self.offsets = [0, 0, 0]
        self.pane, self.split = 0, False
        self.collapsed, self.viewed = set(), set()
        self.query, self.message = '', 'Discovering changed repositories…'
        self.next_scan = time.monotonic() + interval
        self.picker = None
        self.modal_index = 0
        self.input_mode, self.input_value = None, ''
        self.help = False
        self.theme = theme
        self.colors()
        screen.timeout(80)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
        except curses.error:
            pass
        backend.ROOTS = roots
        self.discover()

    def colors(self):
        light = self.theme == 'light'
        bg, selection = (255, 189) if light else (235, 24)
        bar = 254 if light else 236
        ink = 238 if light else 252
        tones = {
            'muted': 244, 'accent': 98 if light else 183,
            'blue': 25 if light else 111, 'cyan': 30 if light else 80,
            'green': 28 if light else 114, 'red': 160 if light else 210,
            'amber': 130 if light else 221, 'pink': 126 if light else 212,
            'purple': 98 if light else 183,
        }
        palette = {'normal': (ink, bg), 'selected': (ink, selection),
                   'bar': (tones['accent'], bar), 'border': (250 if light else 239, bg),
                   'hunk': (61 if light else 147, 253 if light else 237)}
        for name, fg in tones.items():
            palette[name] = (fg, bg)
            palette[name+'_selected'] = (fg, selection)
        for name, tone in [('files','blue'), ('diff','cyan'), ('history','pink')]:
            palette['pane_'+name] = (tones[tone], bg)
            palette['focus_'+name] = (tones[tone], {'files':153,'diff':159,'history':225}[name] if light else {'files':24,'diff':23,'history':53}[name])
        for kind, background, foreground in [('text',bg,ink),
                ('add',194 if light else 22,22 if light else 157),
                ('del',224 if light else 52,88 if light else 217)]:
            palette[kind] = (foreground,background)
            palette[kind+'_gutter'] = (tones['green' if kind=='add' else 'red' if kind=='del' else 'muted'],background)
            for token, tone in [('keyword','purple'),('string','green'),('number','cyan'),('comment','muted'),('decorator','amber')]:
                palette[kind+'_'+token] = (tones[tone],background)
        self.attrs = {name: 0 for name in palette}
        if not curses.has_colors():
            for name in self.attrs:
                if 'selected' in name or name.startswith('focus_'):
                    self.attrs[name] = curses.A_REVERSE
            self.attrs['accent'] = curses.A_BOLD
            return
        curses.start_color()
        basic = {'muted':curses.COLOR_BLUE,'accent':curses.COLOR_MAGENTA,
                 'blue':curses.COLOR_BLUE,'cyan':curses.COLOR_CYAN,
                 'green':curses.COLOR_GREEN,'red':curses.COLOR_RED,
                 'amber':curses.COLOR_YELLOW,'pink':curses.COLOR_MAGENTA,
                 'purple':curses.COLOR_MAGENTA,'add':curses.COLOR_GREEN,
                 'del':curses.COLOR_RED,'hunk':curses.COLOR_CYAN}
        allocated = {}
        for name, pair in palette.items():
            if curses.COLORS < 256:
                fg = basic.get(name.split('_')[0], curses.COLOR_BLACK if light else curses.COLOR_WHITE)
                if name.startswith('pane_'):
                    fg = {'pane_files':curses.COLOR_BLUE,'pane_diff':curses.COLOR_CYAN,'pane_history':curses.COLOR_MAGENTA}[name]
                if name.endswith('_keyword') or name.endswith('_decorator'):
                    fg = curses.COLOR_MAGENTA
                elif name.endswith('_string'):
                    fg = curses.COLOR_GREEN
                elif name.endswith('_number'):
                    fg = curses.COLOR_CYAN
                background = curses.COLOR_WHITE if light else curses.COLOR_BLACK
                if 'selected' in name or name.startswith('focus_'):
                    fg, background = curses.COLOR_WHITE, curses.COLOR_BLUE
                pair = (fg, background)
            if pair not in allocated:
                index = len(allocated)+1
                if index >= curses.COLOR_PAIRS:
                    self.attrs[name] = self.attrs['normal']
                    continue
                curses.init_pair(index, *pair)
                allocated[pair] = curses.color_pair(index)
            self.attrs[name] = allocated[pair]
        self.screen.bkgd(' ', self.attrs['normal'])

    def semantic(self, tone, selected=False):
        return 'selected' if tone=='normal' and selected else tone+'_selected' if selected else tone

    def file_tone(self, path):
        suffix = Path(path).suffix.lower()
        if suffix in ('.py','.sh','.bash'):
            return 'amber'
        if suffix in ('.ts','.tsx','.js','.jsx','.go','.rs','.c','.cpp'):
            return 'blue'
        if suffix in ('.css','.scss','.html','.vue','.svg'):
            return 'pink'
        if suffix in ('.json','.toml','.yaml','.yml','.ini'):
            return 'cyan'
        if suffix in ('.md','.txt','.rst'):
            return 'green'
        return 'normal'

    def put(self, y, x, text, style='normal', size=None, bold=False):
        h, w = self.screen.getmaxyx()
        if y < 0 or y >= h or x < 0 or x >= w:
            return
        try:
            self.screen.addstr(y, x, clip(text, min(w-x-1, size if size is not None else w)),
                               self.attrs[style] | (curses.A_BOLD if bold else 0))
        except curses.error:
            pass

    def fill(self, y, x, size, style='normal'):
        self.put(y, x, ' ' * size, style, size)

    def submit(self, function, callback, label):
        if self.pending:
            self.message = 'Please wait for the current Git operation…'
            return False
        self.pending = self.executor.submit(function)
        self.after = callback
        self.message = label
        return True

    def poll(self):
        if self.pending and self.pending.done():
            future, callback = self.pending, self.after
            self.pending = self.after = None
            try:
                callback(future.result())
            except Exception as exc:
                self.message = str(exc)

    def discover(self):
        if self.pending:
            return
        def worker():
            # Missing default directories are skipped; explicitly passed roots are validated by main.
            backend.ROOTS = [p for p in self.roots if p.is_dir()]
            data = backend.scan()
            return sorted(data['repos'], key=lambda r: r['path'])
        def done(repos):
            self.repos = repos
            self.next_scan = time.monotonic() + self.interval
            self.message = f'{len(repos)} changed repositories · r switch · a add directory'
            if not self.repo and repos:
                self.select_repo(repos[0])
            elif self.repo:
                self.load_state(self.commit, preserve=True)
        self.submit(worker, done, 'Scanning local Git repositories…')

    def select_repo(self, repo):
        if self.pending:
            return
        self.repo = repo
        self.viewed.clear()
        self.collapsed.clear()
        self.query = ''
        self.offsets = [0, 0, 0]
        self.file_index = self.history_index = 0
        self.picker = None
        self.load_state(None)

    def load_state(self, commit=None, preserve=False):
        if not self.repo or self.pending:
            return
        repo = self.repo['id']
        def worker():
            return backend.changed_files(repo, commit), backend.history(repo)
        def done(data):
            self.files, self.commits = data
            self.commit = commit
            if not preserve:
                self.viewed.clear()
                self.file_index = 0
                self.offsets = [0, 0, 0]
            if self.selected not in {f['path'] for f in self.files}:
                self.selected = self.files[0]['path'] if self.files else ''
            self.history_index = next((i+1 for i,c in enumerate(self.commits) if c['id'] == commit), 0)
            self.tree = file_tree(self.files, self.collapsed, self.query)
            self.file_index = next((i for i,r in enumerate(self.tree) if r['path'] == self.selected), 0)
            if self.selected:
                self.load_diff(self.selected, preserve)
            else:
                self.rows = []
                self.message = 'Working tree clean · 3 then Enter to review a commit'
        self.submit(worker, done, 'Reading files and commit history…')

    def load_diff(self, path, preserve=False):
        if not self.repo or self.pending:
            return
        def done(raw):
            self.selected = path
            self.rows = diff_lines(raw)
            if not preserve:
                self.scroll = self.horizontal = 0
            self.message = 'Read-only · nothing is uploaded or modified'
        self.submit(lambda: backend.diff(self.repo['id'], path, self.commit), done, 'Reading diff…')

    def bounds(self):
        h, w = self.screen.getmaxyx()
        left, right = max(24, min(34, w//5)), max(27, min(40, w//4))
        return h, w, left, w-right-1

    def draw(self):
        self.screen.erase()
        h, w, left, right = self.bounds()
        if w < 96 or h < 24:
            self.put(1, 2, 'Gitglass needs at least 96 columns × 24 rows.', 'accent')
            self.put(3, 2, f'Current: {w} × {h} · resize terminal or press q to quit', 'muted')
            self.screen.refresh()
            return
        self.fill(0, 0, w, 'bar')
        self.put(0, 2, '◈ gitglass', 'accent', bold=True)
        name = self.repo['name'] if self.repo else 'Local workspace'
        branch = self.repo['branch'] if self.repo else 'no repository'
        self.put(0, 17, f'{name}  /  {branch}', 'bar', w-38)
        self.put(0, w-18, 'LINUX · LOCAL', 'muted')
        for y in range(1, h-2):
            self.put(y, left, '│', 'border')
            self.put(y, right, '│', 'border')
        headings = [('1  FILES', 0, left), ('2  DIFF', left+1, right-left-1), ('3  HISTORY', right+1, w-right-1)]
        for i, (title, x, size) in enumerate(headings):
            role = ('focus_' if self.pane == i else 'pane_') + ['files','diff','history'][i]
            self.fill(2, x+1, size-2, role)
            self.put(2, x+2, title, role, size-4, True)
        self.draw_files(h, left)
        self.draw_diff(h, left, right)
        self.draw_history(h, w, right)
        self.fill(h-2, 0, w, 'bar')
        spinner = '◐◓◑◒'[int(time.monotonic()*8)%4] if self.pending else '●'
        self.put(h-2, 2, f'{spinner} {self.message}', 'bar', w-4)
        self.put(h-2, 2, spinner, 'amber' if self.pending else 'green', 2)
        self.put(h-1, 2, 'Tab panes   j/k move   Enter open   / filter   r repos   a directory   s split   v viewed   ? help   q quit', 'muted', w-4)
        if self.picker is not None:
            self.draw_picker(h, w)
        if self.help:
            self.draw_help(h, w)
        if self.input_mode:
            self.fill(h-2, 0, w, 'selected')
            label = 'Filter: ' if self.input_mode == 'filter' else 'Scan directory: '
            self.put(h-2, 2, label+self.input_value+'▏', 'selected', w-4)
        self.screen.refresh()

    def draw_files(self, h, left):
        self.put(4, 2, f'{len(self.files)} changed files', 'blue', left-4, True)
        self.put(5, 2, ('⌕ '+self.query) if self.query else 'Local changes · read-only', 'muted', left-4)
        self.tree = file_tree(self.files, self.collapsed, self.query)
        self.file_index = min(self.file_index, max(0, len(self.tree)-1))
        self.offsets[0] = max(0, min(self.offsets[0], self.file_index))
        if self.file_index >= self.offsets[0]+h-10:
            self.offsets[0] = self.file_index-(h-10)+1
        for y, row in enumerate(self.tree[self.offsets[0]:self.offsets[0]+h-10], 7):
            selected = y-7+self.offsets[0] == self.file_index
            style = 'selected' if selected else 'normal'
            if selected:
                self.fill(y, 1, left-2, style)
            symbol = ('▸' if row['path'] in self.collapsed else '▾') if row['folder'] else ('✓' if row['path'] in self.viewed else '·')
            indent = min(row['depth']*2, left//3)
            tone = 'cyan' if row['folder'] else self.file_tone(row['path'])
            self.put(y, 2, ' '*indent+symbol+' '+row['name'], self.semantic(tone,selected), left-7, row['folder'])
            if not row['folder'] and row['path'] in self.viewed:
                self.put(y, 2+indent, symbol, self.semantic('green',selected), 1)
            status_tone = {'M':'amber','A':'green','?':'cyan','D':'red','R':'blue','C':'blue','U':'pink'}.get(row['status'],'muted')
            self.put(y, left-3, row['status'], self.semantic(status_tone,selected), 2, True)
        if not self.tree:
            self.put(8, 2, 'No matching files' if self.query else 'No changes found', 'muted', left-4)
            if not self.repo:
                self.put(10, 2, 'Press a to scan a directory', 'muted', left-4)

    def draw_diff(self, h, left, right):
        x, size = left+3, right-left-5
        title = self.selected or 'Your next commit starts here.'
        self.put(4, x, title, 'accent', size, True)
        label = next((c['short']+' · '+c['message'] for c in self.commits if c['id']==self.commit), 'Uncommitted changes')
        self.put(5, x, label, 'amber' if self.commit else 'green', size)
        count = len({f['path'] for f in self.files} & self.viewed)
        additions = sum(r[0]=='add' for r in self.rows)
        deletions = sum(r[0]=='del' for r in self.rows)
        metrics = [(f'+{additions}', 'green'), (f'  −{deletions}', 'red'),
                   (f'    {count}/{len(self.files)} viewed', 'purple'),
                   (f'    {"SPLIT" if self.split else "UNIFIED"}', 'cyan')]
        cursor = x
        for text,tone in metrics:
            self.put(6,cursor,text,tone,max(0,x+size-cursor))
            cursor += width(text)
        visible = paired_rows(self.rows) if self.split else self.rows
        self.scroll = max(0, min(self.scroll, max(0,len(visible)-(h-11))))
        for y, row in enumerate(visible[self.scroll:self.scroll+h-11], 8):
            if self.split:
                half = (size-1)//2
                self.code_row(y, x, half, row[0], True, False)
                self.put(y, x+half, '│', 'border')
                self.code_row(y, x+half+1, size-half-1, row[1], True, True)
            else:
                self.code_row(y, x, size, row)
        if not self.rows:
            self.put(10, x, 'Select a file to review its diff.', 'muted', size)
            self.put(12, x, 'r  Switch repository    a  Add scan directory', 'muted', size)
        self.put(h-3, x, f'{self.scroll+1 if visible else 0}/{len(visible)} lines  ·  h/l horizontal scroll', 'muted', size)

    def code_row(self, y, x, size, row, split=False, new=False):
        if row is None:
            return
        kind, old, next_line, text = row
        style = {'add':'add','del':'del','hunk':'hunk'}.get(kind,'text')
        self.fill(y, x, size, style)
        if kind == 'hunk':
            self.put(y, x+1, text, style, size-2)
            return
        prefix = f'{str(next_line if new else old):>4} ' if split else f'{str(old):>4} {str(next_line):>4} '
        prefix += '+ ' if kind=='add' else '− ' if kind=='del' else '  '
        self.put(y, x, prefix, style+'_gutter', size)
        available = size-width(prefix)
        if available > 0:
            self.syntax(y, x+width(prefix), text, style, available)

    def syntax(self, y, x, text, style, available):
        # Lightweight token colors preserve the original green/red Diff backgrounds.
        tokens = re.finditer(r"(?P<comment>\#.*$|//.*$)|(?P<string>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')|(?P<decorator>@[\w.]+)|(?P<keyword>\b(?:def|class|import|from|as|return|if|else|elif|for|while|in|try|except|finally|with|async|await|const|let|var|function|export|default|new|true|false|null|None|True|False|fn|pub|use|struct|impl|match|package|func|type|interface)\b)|(?P<number>\b(?:0x[0-9a-fA-F]+|\d+(?:\.\d+)?)\b)", text)
        self.put(y,x,clip(text,available,self.horizontal),style,available)
        for token in tokens:
            start = width(text[:token.start()])
            end = start+width(token.group())
            left = max(start,self.horizontal)
            right = min(end,self.horizontal+available)
            if right > left:
                self.put(y,x+left-self.horizontal,clip(token.group(),right-left,left-start),
                         style+'_'+token.lastgroup,right-left)

    def draw_history(self, h, w, right):
        x, size = right+3, w-right-5
        self.put(4, x, f'{len(self.commits)} recent commits', 'pink', size, True)
        items = [None] + self.commits
        capacity = max(1,(h-9)//3)
        self.history_index = min(self.history_index,len(items)-1)
        self.offsets[2] = min(self.offsets[2],self.history_index)
        if self.history_index >= self.offsets[2]+capacity:
            self.offsets[2] = self.history_index-capacity+1
        for n, item in enumerate(items[self.offsets[2]:self.offsets[2]+capacity]):
            y = 6+n*3
            index = self.offsets[2]+n
            selected = index == self.history_index
            current = (item is None and self.commit is None) or (item is not None and item['id']==self.commit)
            style = 'selected' if selected else 'normal'
            if selected:
                self.fill(y, x-1, size+1, style)
                self.fill(y+1, x-1, size+1, style)
            message = item['message'] if item else 'Uncommitted changes'
            category = message.split(':',1)[0].split('(',1)[0]
            tone = {'feat':'green','fix':'amber','refactor':'cyan','style':'pink','docs':'blue','test':'purple','chore':'muted'}.get(category,'normal') if item else 'purple'
            self.put(y,x,('● ' if current else '○ '),self.semantic('green' if current else tone,selected),2)
            self.put(y,x+2,message,self.semantic(tone,selected),size-2,current)
            if item:
                self.put(y+1,x+2,item['short'],self.semantic('amber',selected),size-2)
                self.put(y+1,x+10,' · '+item['author'],self.semantic('blue',selected),size-10)
            else:
                self.put(y+1,x+2,'Working tree',self.semantic('purple',selected),size-2)
        self.put(h-3, x, 'Enter review · u working tree', 'muted', size)

    def box(self, title, lines, h, w):
        bw = min(w-6, 78)
        bh = min(h-6, len(lines)+4)
        x,y = (w-bw)//2,(h-bh)//2
        for n in range(bh):
            self.fill(y+n,x,bw,'bar')
        self.put(y,x,'┌'+'─'*(bw-2)+'┐','accent',bw)
        self.put(y+bh-1,x,'└'+'─'*(bw-2)+'┘','accent',bw)
        self.put(y+1,x+2,title,'accent',bw-4,True)
        for i,line in enumerate(lines[:bh-4]):
            self.put(y+3+i,x+2,line,'bar',bw-4)
        return x,y,bw,bh

    def draw_picker(self,h,w):
        self.modal_index = min(self.modal_index,max(0,len(self.repos)-1))
        capacity = min(12,h-12)
        start = max(0,self.modal_index-capacity+1)
        lines = [('› ' if i==self.modal_index else '  ')+r['name']+'  '+r['path'] for i,r in enumerate(self.repos) if start<=i<start+capacity]
        if not lines:
            lines = ['No changed repositories. Press Esc, then a to add a directory.']
        lines += ['', 'j/k select · Enter open · Esc close']
        self.box('LOCAL REPOSITORIES', lines,h,w)

    def draw_help(self,h,w):
        self.box('GITGLASS / KEYBOARD',[
            'Tab / Shift-Tab     Focus next / previous pane',
            '1 / 2 / 3           Focus files / diff / history',
            'j k / ↑ ↓           Move selection or scroll diff',
            'Enter / Space       Open file, toggle folder, review commit',
            'PgUp / PgDn         Scroll diff a page',
            'h l / ← →           Scroll diff horizontally',
            '/                   Filter files (Enter apply, Esc cancel)',
            'r / a               Switch repository / add scan directory',
            's / v               Unified or split diff / mark file viewed',
            'u / R               Return to working tree / refresh',
            'g / G               Top / bottom of current pane',
            '? / Esc             Close help',
            'q                   Quit · all Git operations are read-only',
        ],h,w)

    def move(self, delta):
        if self.picker is not None:
            self.modal_index = max(0,min(len(self.repos)-1,self.modal_index+delta))
        elif self.pane==0:
            self.file_index = max(0,min(len(getattr(self,'tree',[]))-1,self.file_index+delta))
        elif self.pane==1:
            self.scroll = max(0,self.scroll+delta)
        else:
            self.history_index = max(0,min(len(self.commits),self.history_index+delta))

    def enter(self):
        if self.picker is not None:
            if self.repos:
                self.select_repo(self.repos[self.modal_index])
        elif self.pane==0 and getattr(self,'tree',[]):
            row = self.tree[self.file_index]
            if row['folder']:
                if row['path'] in self.collapsed:
                    self.collapsed.remove(row['path'])
                else:
                    self.collapsed.add(row['path'])
            else:
                self.load_diff(row['path'])
        elif self.pane==2:
            self.load_state(self.commits[self.history_index-1]['id'] if self.history_index else None)

    def input_key(self,key):
        if key=='\x1b':
            self.input_mode = None
        elif key in ('\n','\r',curses.KEY_ENTER):
            if self.input_mode=='filter':
                self.query = self.input_value
                self.file_index = self.offsets[0] = 0
            else:
                path = Path(self.input_value).expanduser().resolve()
                if path.is_dir():
                    if path not in self.roots:
                        self.roots.append(path)
                    self.discover()
                else:
                    self.message = 'Directory does not exist: '+str(path)
            self.input_mode = None
        elif key in (curses.KEY_BACKSPACE,'\x7f','\b'):
            self.input_value = self.input_value[:-1]
        elif key=='\x15':
            self.input_value = ''
        elif isinstance(key,str) and key.isprintable():
            self.input_value += key

    def mouse(self):
        try:
            _,x,y,_,state = curses.getmouse()
        except curses.error:
            return
        h,w,left,right = self.bounds()
        self.pane = 0 if x<left else 2 if x>right else 1
        if state & curses.BUTTON4_PRESSED:
            self.move(-3)
        elif state & getattr(curses,'BUTTON5_PRESSED',0):
            self.move(3)
        elif state & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED):
            if self.pane==0 and 7<=y<h-3:
                self.file_index = min(max(0,len(getattr(self,'tree',[]))-1),self.offsets[0]+y-7)
                self.enter()
            elif self.pane==2 and 6<=y<h-3:
                self.history_index = min(len(self.commits),self.offsets[2]+(y-6)//3)
                self.enter()

    def run(self):
        try:
            while True:
                self.poll()
                if time.monotonic()>=self.next_scan and not self.pending and not self.input_mode and self.picker is None:
                    self.discover()
                self.draw()
                try:
                    key = self.screen.get_wch()
                except curses.error:
                    continue
                if self.input_mode:
                    self.input_key(key); continue
                if self.help:
                    if key in ('?','\x1b','q'):
                        self.help = False
                    continue
                if key=='\x1b':
                    self.picker = None; continue
                if key=='q':
                    break
                if key in ('j',curses.KEY_DOWN):
                    self.move(1)
                elif key in ('k',curses.KEY_UP):
                    self.move(-1)
                elif key in ('\n','\r',' ',curses.KEY_ENTER):
                    self.enter()
                elif self.picker is not None:
                    continue
                elif key=='\t':
                    self.pane = (self.pane+1)%3
                elif key==curses.KEY_BTAB:
                    self.pane = (self.pane-1)%3
                elif key in ('1','2','3'):
                    self.pane = int(key)-1
                elif key in (curses.KEY_NPAGE,curses.KEY_PPAGE):
                    self.pane = 1
                    self.scroll = max(0,self.scroll+(1 if key==curses.KEY_NPAGE else -1)*(self.screen.getmaxyx()[0]-12))
                elif key in ('h','l',curses.KEY_LEFT,curses.KEY_RIGHT):
                    self.horizontal = max(0,self.horizontal+(-8 if key in ('h',curses.KEY_LEFT) else 8))
                elif key in ('g','G'):
                    self.move(-100000 if key=='g' else 100000)
                elif key=='/':
                    self.input_mode,self.input_value = 'filter',self.query
                elif key=='a':
                    self.input_mode,self.input_value = 'root',''
                elif key=='r':
                    self.picker,self.modal_index = True,0
                elif key=='R':
                    self.discover()
                elif key=='s':
                    self.split = not self.split
                    self.scroll = 0
                elif key=='v' and self.selected:
                    if self.selected in self.viewed:
                        self.viewed.remove(self.selected)
                    else:
                        self.viewed.add(self.selected)
                elif key=='u':
                    self.load_state(None)
                elif key=='?':
                    self.help = True
                elif key==curses.KEY_MOUSE and not self.pending:
                    self.mouse()
        finally:
            # Git subprocesses have a 15 second timeout; no scan writes repository data.
            self.executor.shutdown(wait=False,cancel_futures=True)


def main():
    parser = argparse.ArgumentParser(prog='gitglass', description='Gitglass — local Git diff terminal viewer')
    parser.add_argument('directories', nargs='*', help='Directories to scan (default: current directory)')
    parser.add_argument('--root', action='append', default=[], help='Additional scan directory; repeatable')
    parser.add_argument('--interval', type=int, default=30, help='Auto-refresh seconds (minimum 5)')
    parser.add_argument('--theme', choices=['light','dark'], default='light')
    parser.add_argument('--version', action='version', version='gitglass 0.2.0')
    args = parser.parse_args()
    if not shutil.which('git'):
        parser.error('Git is required. Install Git and try again.')
    roots = list(dict.fromkeys(Path(p).expanduser().resolve() for p in (args.directories+args.root or [str(Path.cwd())])))
    for root in roots:
        if not root.is_dir():
            parser.error(f'Directory does not exist: {root}')
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error('Run gitglass in an interactive terminal (not through a pipe).')
    locale.setlocale(locale.LC_ALL,'')
    try:
        curses.wrapper(lambda screen: App(screen,roots,max(5,args.interval),args.theme).run())
    except KeyboardInterrupt:
        pass
    except curses.error as exc:
        print(f'Terminal error: {exc}. Try TERM=xterm-256color gitglass.',file=sys.stderr)
        return 1
    return 0


if __name__=='__main__':
    sys.exit(main())
