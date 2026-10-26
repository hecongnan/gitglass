"""Regression coverage for launching Gitglass in a clean, already-pushed repo."""
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server as backend
import tui


class CleanRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / 'project'
        self.repo.mkdir()
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Gitglass Test')
        self.git('config', 'user.email', 'gitglass@example.invalid')
        (self.repo / 'src').mkdir()
        (self.repo / 'src' / 'viewer.py').write_text('def title():\n    return "preview-test"\n')
        (self.repo / '说明.md').write_text('# 已提交的文件\n')
        self.git('add', '.')
        self.git('commit', '-m', 'feat: clean repository regression')
        # Reproduce a repository that has already been pushed, without using the network.
        self.remote = self.base / 'remote.git'
        subprocess.run(['git', 'init', '--bare', str(self.remote)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.git('remote', 'add', 'origin', str(self.remote))
        self.git('push', '-u', 'origin', 'main')
        self.original_roots = backend.ROOTS
        backend.ROOTS = [self.repo]
        backend.REPOS.clear()
        backend.KNOWN.clear()
        self.addCleanup(setattr, backend, 'ROOTS', self.original_roots)
        self.addCleanup(backend.REPOS.clear)
        self.addCleanup(backend.KNOWN.clear)

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args],
                                       stderr=subprocess.DEVNULL).decode().strip()

    def test_clean_pushed_root_is_discovered(self):
        repos = backend.scan(include_root_repos=True)['repos']
        self.assertEqual(len(repos), 1)
        self.assertEqual(repos[0]['id'], str(self.repo))
        self.assertTrue(repos[0]['explicit'])
        self.assertFalse(repos[0]['has_changes'])
        self.assertEqual(backend.history(self.repo)[0]['message'], 'feat: clean repository regression')

    def test_launching_inside_nested_directory_resolves_worktree_root(self):
        backend.ROOTS = [self.repo / 'src']
        self.assertEqual(backend.scan(include_root_repos=True)['repos'][0]['id'], str(self.repo))

    def test_parent_scan_still_discovers_only_changed_children(self):
        backend.ROOTS = [self.base]
        self.assertEqual(backend.scan(include_root_repos=True)['repos'], [])
        (self.repo / 'src' / 'viewer.py').write_text('changed\n')
        repos = backend.scan(include_root_repos=True)['repos']
        self.assertEqual(len(repos), 1)
        self.assertTrue(repos[0]['has_changes'])
        self.assertFalse(repos[0]['explicit'])

    def test_clean_worktree_git_file(self):
        worktree = self.base / 'linked'
        self.git('worktree', 'add', '-b', 'linked', str(worktree))
        backend.ROOTS = [worktree / 'src']
        self.assertTrue((worktree / '.git').is_file())
        self.assertEqual(backend.scan(include_root_repos=True)['repos'][0]['id'], str(worktree))

    def test_tracked_preview_is_context_not_added_content(self):
        files = backend.tracked_files(self.repo)
        self.assertEqual({f['path'] for f in files}, {'src/viewer.py', '说明.md'})
        rows = tui.diff_lines(backend.preview_file(self.repo, '说明.md'))
        self.assertEqual(rows[-1], ('text', 1, 1, '# 已提交的文件'))
        self.assertFalse(any(row[0] in ('add', 'del') for row in rows))
        self.assertEqual(backend.changed_files(self.repo), [])

    def test_preview_binary_empty_and_symlink_are_safe(self):
        (self.repo / 'binary').write_bytes(b'x\0y')
        self.assertIn('Binary', backend.preview_file(self.repo, 'binary'))
        (self.repo / 'empty').touch()
        self.assertEqual(backend.preview_file(self.repo, 'empty'), 'Empty file')
        outside = self.base / 'outside.txt'
        outside.write_text('do not follow symlinks')
        (self.repo / 'link').symlink_to(outside)
        preview = backend.preview_file(self.repo, 'link')
        self.assertIn(str(outside), preview)
        self.assertNotIn('do not follow symlinks', preview)
        with self.assertRaises(ValueError):
            backend.preview_file(self.repo, '../outside.txt')

    def test_tui_switches_between_clean_preview_dirty_diff_and_history(self):
        with patch.object(tui.App, 'colors'), patch.object(tui.curses, 'curs_set'), patch.object(tui.curses, 'mousemask'):
            app = tui.App(Mock(), [self.repo])
        self.addCleanup(app.executor.shutdown, wait=True, cancel_futures=True)
        def settle():
            deadline = time.monotonic() + 5
            while app.pending and time.monotonic() < deadline:
                app.poll()
                time.sleep(.01)
            self.assertIsNone(app.pending, app.message)
        settle()
        self.assertIsNotNone(app.repo)
        self.assertTrue(app.all_files)
        self.assertEqual(len(app.files), 2)
        self.assertEqual(len(app.commits), 1)
        self.assertFalse(any(r[0] in ('add', 'del') for r in app.rows))
        (self.repo / 'src' / 'viewer.py').write_text('changed\n')
        app.load_state(preserve=True)
        settle()
        self.assertFalse(app.all_files)
        self.assertEqual([f['path'] for f in app.files], ['src/viewer.py'])
        self.assertTrue(any(r[0] == 'add' for r in app.rows))
        self.git('checkout', '--', 'src/viewer.py')
        app.load_state(preserve=True)
        settle()
        self.assertTrue(app.all_files)
        app.load_state(app.commits[0]['id'])
        settle()
        self.assertFalse(app.all_files)
        self.assertTrue(any(r[0] == 'add' for r in app.rows))
        app.load_state(None)
        settle()
        self.assertTrue(app.all_files)

    @unittest.skipUnless(sys.platform == 'linux', 'requires a Linux pseudo-terminal')
    def test_real_terminal_from_clean_repo_subdirectory(self):
        import fcntl
        import pty
        import struct
        import termios
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 150, 0, 0))
        launcher = Path(tui.__file__).with_name('gitglass')
        process = subprocess.Popen([str(launcher)], cwd=self.repo / 'src',
                                   stdin=slave, stdout=slave, stderr=slave,
                                   env={**os.environ, 'TERM': 'xterm-256color'})
        os.close(slave)
        data = b''
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if select.select([master], [], [], .1)[0]:
                    try:
                        data += os.read(master, 65536)
                    except OSError:
                        break
                if b'READ-ONLY PREVIEW' in data and b'feat: clean repository' in data:
                    break
            # curses reuses the unchanged 'ed files' suffix after its first frame.
            self.assertIn(b'2 track', data)
            self.assertIn(b'viewer.py', data)
            self.assertIn(b'READ-ONLY PREVIEW', data)
            self.assertIn(b'feat: clean repository', data)
            os.write(master, b'q')
            process.wait(timeout=5)
            self.assertEqual(process.returncode, 0, data.decode(errors='replace'))
            self.assertNotIn(b'Traceback', data)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)


if __name__ == '__main__':
    unittest.main()
