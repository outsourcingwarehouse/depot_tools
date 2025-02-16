#!/usr/bin/env vpython3
# coding=utf-8
# Copyright (c) 2012 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Unit tests for git_cl.py."""

from __future__ import print_function
from __future__ import unicode_literals

import datetime
import json
import logging
import multiprocessing
import optparse
import os
import pprint
import shutil
import sys
import tempfile
import unittest

if sys.version_info.major == 2:
  from StringIO import StringIO
  import mock
else:
  from io import StringIO
  from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import metrics
# We have to disable monitoring before importing git_cl.
metrics.DISABLE_METRICS_COLLECTION = True

import clang_format
import contextlib
import gclient_utils
import gerrit_util
import git_cl
import git_common
import git_footers
import git_new_branch
import scm
import subprocess2

NETRC_FILENAME = '_netrc' if sys.platform == 'win32' else '.netrc'


def callError(code=1, cmd='', cwd='', stdout=b'', stderr=b''):
  return subprocess2.CalledProcessError(code, cmd, cwd, stdout, stderr)

CERR1 = callError(1)


class TemporaryFileMock(object):
  def __init__(self):
    self.suffix = 0

  @contextlib.contextmanager
  def __call__(self):
    self.suffix += 1
    yield '/tmp/fake-temp' + str(self.suffix)


class ChangelistMock(object):
  # A class variable so we can access it when we don't have access to the
  # instance that's being set.
  desc = ''

  def __init__(self, gerrit_change=None, **kwargs):
    self._gerrit_change = gerrit_change

  def GetIssue(self):
    return 1

  def FetchDescription(self):
    return ChangelistMock.desc

  def UpdateDescription(self, desc, force=False):
    ChangelistMock.desc = desc

  def GetGerritChange(self, patchset=None, **kwargs):
    del patchset
    return self._gerrit_change


class GitMocks(object):
  def __init__(self, config=None, branchref=None):
    self.branchref = branchref or 'refs/heads/master'
    self.config = config or {}

  def GetBranchRef(self, _root):
    return self.branchref

  def NewBranch(self, branchref):
    self.branchref = branchref

  def GetConfig(self, root, key, default=None):
    if root != '':
      key = '%s:%s' % (root, key)
    return self.config.get(key, default)

  def SetConfig(self, root, key, value=None):
    if root != '':
      key = '%s:%s' % (root, key)
    if value:
      self.config[key] = value
      return
    if key not in self.config:
      raise CERR1
    del self.config[key]


class WatchlistsMock(object):
  def __init__(self, _):
    pass
  @staticmethod
  def GetWatchersForPaths(_):
    return ['joe@example.com']


class CodereviewSettingsFileMock(object):
  def __init__(self):
    pass
  # pylint: disable=no-self-use
  def read(self):
    return ('CODE_REVIEW_SERVER: gerrit.chromium.org\n' +
            'GERRIT_HOST: True\n')


class AuthenticatorMock(object):
  def __init__(self, *_args):
    pass
  def has_cached_credentials(self):
    return True
  def authorize(self, http):
    return http


def CookiesAuthenticatorMockFactory(hosts_with_creds=None, same_auth=False):
  """Use to mock Gerrit/Git credentials from ~/.netrc or ~/.gitcookies.

  Usage:
    >>> self.mock(git_cl.gerrit_util, "CookiesAuthenticator",
                  CookiesAuthenticatorMockFactory({'host': ('user', _, 'pass')})

  OR
    >>> self.mock(git_cl.gerrit_util, "CookiesAuthenticator",
                  CookiesAuthenticatorMockFactory(
                      same_auth=('user', '', 'pass'))
  """
  class CookiesAuthenticatorMock(git_cl.gerrit_util.CookiesAuthenticator):
    def __init__(self):  # pylint: disable=super-init-not-called
      # Intentionally not calling super() because it reads actual cookie files.
      pass
    @classmethod
    def get_gitcookies_path(cls):
      return os.path.join('~', '.gitcookies')

    @classmethod
    def get_netrc_path(cls):
      return os.path.join('~', NETRC_FILENAME)

    def _get_auth_for_host(self, host):
      if same_auth:
        return same_auth
      return (hosts_with_creds or {}).get(host)
  return CookiesAuthenticatorMock


class MockChangelistWithBranchAndIssue():
  def __init__(self, branch, issue):
    self.branch = branch
    self.issue = issue
  def GetBranch(self):
    return self.branch
  def GetIssue(self):
    return self.issue


class SystemExitMock(Exception):
  pass


class TestGitClBasic(unittest.TestCase):
  def setUp(self):
    mock.patch('sys.exit', side_effect=SystemExitMock).start()
    mock.patch('sys.stdout', StringIO()).start()
    mock.patch('sys.stderr', StringIO()).start()
    self.addCleanup(mock.patch.stopall)

  def test_die_with_error(self):
    with self.assertRaises(SystemExitMock):
      git_cl.DieWithError('foo', git_cl.ChangeDescription('lorem ipsum'))
    self.assertEqual(sys.stderr.getvalue(), 'foo\n')
    self.assertTrue('saving CL description' in sys.stdout.getvalue())
    self.assertTrue('Content of CL description' in sys.stdout.getvalue())
    self.assertTrue('lorem ipsum' in sys.stdout.getvalue())
    sys.exit.assert_called_once_with(1)

  def test_die_with_error_no_desc(self):
    with self.assertRaises(SystemExitMock):
      git_cl.DieWithError('foo')
    self.assertEqual(sys.stderr.getvalue(), 'foo\n')
    self.assertEqual(sys.stdout.getvalue(), '')
    sys.exit.assert_called_once_with(1)

  def test_fetch_description(self):
    cl = git_cl.Changelist(issue=1, codereview_host='host')
    cl.description = 'x'
    self.assertEqual(cl.FetchDescription(), 'x')

  @mock.patch('git_cl.Changelist.EnsureAuthenticated')
  @mock.patch('git_cl.Changelist.GetStatus', lambda cl: cl.status)
  def test_get_cl_statuses(self, *_mocks):
    statuses = [
        'closed', 'commit', 'dry-run', 'lgtm', 'reply', 'unsent', 'waiting']
    changes = []
    for status in statuses:
      cl = git_cl.Changelist()
      cl.status = status
      changes.append(cl)

    actual = set(git_cl.get_cl_statuses(changes, True))
    self.assertEqual(set(zip(changes, statuses)), actual)

  def test_get_cl_statuses_no_changes(self):
    self.assertEqual([], list(git_cl.get_cl_statuses([], True)))

  @mock.patch('git_cl.Changelist.EnsureAuthenticated')
  @mock.patch('multiprocessing.pool.ThreadPool')
  def test_get_cl_statuses_timeout(self, *_mocks):
    changes = [git_cl.Changelist() for _ in range(2)]
    pool = multiprocessing.pool.ThreadPool()
    it = pool.imap_unordered.return_value.__iter__ = mock.Mock()
    it.return_value.next.side_effect = [
        (changes[0], 'lgtm'),
        multiprocessing.TimeoutError,
    ]

    actual = list(git_cl.get_cl_statuses(changes, True))
    self.assertEqual([(changes[0], 'lgtm'), (changes[1], 'error')], actual)

  @mock.patch('git_cl.Changelist.GetIssueURL')
  def test_get_cl_statuses_not_finegrained(self, _mock):
    changes = [git_cl.Changelist() for _ in range(2)]
    urls = ['some-url', None]
    git_cl.Changelist.GetIssueURL.side_effect = urls

    actual = set(git_cl.get_cl_statuses(changes, False))
    self.assertEqual(
        set([(changes[0], 'waiting'), (changes[1], 'error')]), actual)

  def test_get_issue_url(self):
    cl = git_cl.Changelist(issue=123)
    cl._gerrit_server = 'https://example.com'
    self.assertEqual(cl.GetIssueURL(), 'https://example.com/123')
    self.assertEqual(cl.GetIssueURL(short=True), 'https://example.com/123')

    cl = git_cl.Changelist(issue=123)
    cl._gerrit_server = 'https://chromium-review.googlesource.com'
    self.assertEqual(cl.GetIssueURL(),
                     'https://chromium-review.googlesource.com/123')
    self.assertEqual(cl.GetIssueURL(short=True), 'https://crrev.com/c/123')

  def test_set_preserve_tryjobs(self):
    d = git_cl.ChangeDescription('Simple.')
    d.set_preserve_tryjobs()
    self.assertEqual(d.description.splitlines(), [
      'Simple.',
      '',
      'Cq-Do-Not-Cancel-Tryjobs: true',
    ])
    before = d.description
    d.set_preserve_tryjobs()
    self.assertEqual(before, d.description)

    d = git_cl.ChangeDescription('\n'.join([
      'One is enough',
      '',
      'Cq-Do-Not-Cancel-Tryjobs: dups not encouraged, but don\'t hurt',
      'Change-Id: Ideadbeef',
    ]))
    d.set_preserve_tryjobs()
    self.assertEqual(d.description.splitlines(), [
      'One is enough',
      '',
      'Cq-Do-Not-Cancel-Tryjobs: dups not encouraged, but don\'t hurt',
      'Change-Id: Ideadbeef',
      'Cq-Do-Not-Cancel-Tryjobs: true',
    ])

  def test_get_bug_line_values(self):
    f = lambda p, bugs: list(git_cl._get_bug_line_values(p, bugs))
    self.assertEqual(f('', ''), [])
    self.assertEqual(f('', '123,v8:456'), ['123', 'v8:456'])
    self.assertEqual(f('v8', '456'), ['v8:456'])
    self.assertEqual(f('v8', 'chromium:123,456'), ['v8:456', 'chromium:123'])
    # Not nice, but not worth carying.
    self.assertEqual(f('v8', 'chromium:123,456,v8:123'),
                     ['v8:456', 'chromium:123', 'v8:123'])

  @mock.patch('gerrit_util.GetAccountDetails')
  def test_valid_accounts(self, mockGetAccountDetails):
    mock_per_account = {
      'u1': None,  # 404, doesn't exist.
      'u2': {
        '_account_id': 123124,
        'avatars': [],
        'email': 'u2@example.com',
        'name': 'User Number 2',
        'status': 'OOO',
      },
      'u3': git_cl.gerrit_util.GerritError(500, 'retries didn\'t help :('),
    }
    def GetAccountDetailsMock(_, account):
      # Poor-man's mock library's side_effect.
      v = mock_per_account.pop(account)
      if isinstance(v, Exception):
        raise v
      return v

    mockGetAccountDetails.side_effect = GetAccountDetailsMock
    actual = git_cl.gerrit_util.ValidAccounts(
        'host', ['u1', 'u2', 'u3'], max_threads=1)
    self.assertEqual(actual, {
      'u2': {
        '_account_id': 123124,
        'avatars': [],
        'email': 'u2@example.com',
        'name': 'User Number 2',
        'status': 'OOO',
      },
    })


class TestParseIssueURL(unittest.TestCase):
  def _test(self, arg, issue=None, patchset=None, hostname=None, fail=False):
    parsed = git_cl.ParseIssueNumberArgument(arg)
    self.assertIsNotNone(parsed)
    if fail:
      self.assertFalse(parsed.valid)
      return
    self.assertTrue(parsed.valid)
    self.assertEqual(parsed.issue, issue)
    self.assertEqual(parsed.patchset, patchset)
    self.assertEqual(parsed.hostname, hostname)

  def test_basic(self):
    self._test('123', 123)
    self._test('', fail=True)
    self._test('abc', fail=True)
    self._test('123/1', fail=True)
    self._test('123a', fail=True)
    self._test('ssh://chrome-review.source.com/#/c/123/4/', fail=True)
    self._test('ssh://chrome-review.source.com/c/123/1/', fail=True)

  def test_gerrit_url(self):
    self._test('https://codereview.source.com/123', 123, None,
               'codereview.source.com')
    self._test('http://chrome-review.source.com/c/123', 123, None,
               'chrome-review.source.com')
    self._test('https://chrome-review.source.com/c/123/', 123, None,
               'chrome-review.source.com')
    self._test('https://chrome-review.source.com/c/123/4', 123, 4,
               'chrome-review.source.com')
    self._test('https://chrome-review.source.com/#/c/123/4', 123, 4,
               'chrome-review.source.com')
    self._test('https://chrome-review.source.com/c/123/4', 123, 4,
               'chrome-review.source.com')
    self._test('https://chrome-review.source.com/123', 123, None,
               'chrome-review.source.com')
    self._test('https://chrome-review.source.com/123/4', 123, 4,
               'chrome-review.source.com')

    self._test('https://chrome-review.source.com/bad/123/4', fail=True)
    self._test('https://chrome-review.source.com/c/123/1/whatisthis', fail=True)
    self._test('https://chrome-review.source.com/c/abc/', fail=True)

  def test_short_urls(self):
    self._test('https://crrev.com/c/2151934', 2151934, None,
               'chromium-review.googlesource.com')


class GitCookiesCheckerTest(unittest.TestCase):
  def setUp(self):
    super(GitCookiesCheckerTest, self).setUp()
    self.c = git_cl._GitCookiesChecker()
    self.c._all_hosts = []
    mock.patch('sys.stdout', StringIO()).start()
    self.addCleanup(mock.patch.stopall)

  def mock_hosts_creds(self, subhost_identity_pairs):
    def ensure_googlesource(h):
      if not h.endswith(self.c._GOOGLESOURCE):
        assert not h.endswith('.')
        return h + '.' + self.c._GOOGLESOURCE
      return h
    self.c._all_hosts = [(ensure_googlesource(h), i, '.gitcookies')
                         for h, i in subhost_identity_pairs]

  def test_identity_parsing(self):
    self.assertEqual(self.c._parse_identity('ldap.google.com'),
                     ('ldap', 'google.com'))
    self.assertEqual(self.c._parse_identity('git-ldap.example.com'),
                     ('ldap', 'example.com'))
    # Specical case because we know there are no subdomains in chromium.org.
    self.assertEqual(self.c._parse_identity('git-note.period.chromium.org'),
                     ('note.period', 'chromium.org'))
    # Pathological: ".period." can be either username OR domain, more likely
    # domain.
    self.assertEqual(self.c._parse_identity('git-note.period.example.com'),
                     ('note', 'period.example.com'))

  def test_analysis_nothing(self):
    self.c._all_hosts = []
    self.assertFalse(self.c.has_generic_host())
    self.assertEqual(set(), self.c.get_conflicting_hosts())
    self.assertEqual(set(), self.c.get_duplicated_hosts())
    self.assertEqual(set(), self.c.get_partially_configured_hosts())
    self.assertEqual(set(), self.c.get_hosts_with_wrong_identities())

  def test_analysis(self):
    self.mock_hosts_creds([
      ('.googlesource.com',      'git-example.chromium.org'),

      ('chromium',               'git-example.google.com'),
      ('chromium-review',        'git-example.google.com'),
      ('chrome-internal',        'git-example.chromium.org'),
      ('chrome-internal-review', 'git-example.chromium.org'),
      ('conflict',               'git-example.google.com'),
      ('conflict-review',        'git-example.chromium.org'),
      ('dup',                    'git-example.google.com'),
      ('dup',                    'git-example.google.com'),
      ('dup-review',             'git-example.google.com'),
      ('partial',                'git-example.google.com'),
      ('gpartial-review',        'git-example.google.com'),
    ])
    self.assertTrue(self.c.has_generic_host())
    self.assertEqual(set(['conflict.googlesource.com']),
                     self.c.get_conflicting_hosts())
    self.assertEqual(set(['dup.googlesource.com']),
                     self.c.get_duplicated_hosts())
    self.assertEqual(set(['partial.googlesource.com',
                          'gpartial-review.googlesource.com']),
                     self.c.get_partially_configured_hosts())
    self.assertEqual(set(['chromium.googlesource.com',
                          'chrome-internal.googlesource.com']),
                     self.c.get_hosts_with_wrong_identities())

  def test_report_no_problems(self):
    self.test_analysis_nothing()
    self.assertFalse(self.c.find_and_report_problems())
    self.assertEqual(sys.stdout.getvalue(), '')

  @mock.patch(
      'git_cl.gerrit_util.CookiesAuthenticator.get_gitcookies_path',
      return_value=os.path.join('~', '.gitcookies'))
  def test_report(self, *_mocks):
    self.test_analysis()
    self.assertTrue(self.c.find_and_report_problems())
    with open(os.path.join(os.path.dirname(__file__),
                           'git_cl_creds_check_report.txt')) as f:
      expected = f.read() % {
          'sep': os.sep,
      }

    def by_line(text):
      return [l.rstrip() for l in text.rstrip().splitlines()]
    self.maxDiff = 10000  # pylint: disable=attribute-defined-outside-init
    self.assertEqual(by_line(sys.stdout.getvalue().strip()), by_line(expected))


class TestGitCl(unittest.TestCase):
  def setUp(self):
    super(TestGitCl, self).setUp()
    self.calls = []
    self._calls_done = []
    self.failed = False
    mock.patch('sys.stdout', StringIO()).start()
    mock.patch(
        'git_cl.time_time',
        lambda: self._mocked_call('time.time')).start()
    mock.patch(
        'git_cl.metrics.collector.add_repeated',
        lambda *a: self._mocked_call('add_repeated', *a)).start()
    mock.patch('subprocess2.call', self._mocked_call).start()
    mock.patch('subprocess2.check_call', self._mocked_call).start()
    mock.patch('subprocess2.check_output', self._mocked_call).start()
    mock.patch(
        'subprocess2.communicate',
        lambda *a, **_k: ([self._mocked_call(*a), ''], 0)).start()
    mock.patch(
        'git_cl.gclient_utils.CheckCallAndFilter',
        self._mocked_call).start()
    mock.patch('git_common.is_dirty_git_tree', lambda x: False).start()
    mock.patch(
        'git_common.get_or_create_merge_base',
        lambda *a: self._mocked_call('get_or_create_merge_base', *a)).start()
    mock.patch('git_cl.FindCodereviewSettingsFile', return_value='').start()
    mock.patch(
        'git_cl.SaveDescriptionBackup',
        lambda _: self._mocked_call('SaveDescriptionBackup')).start()
    mock.patch(
        'git_cl.write_json',
        lambda *a: self._mocked_call('write_json', *a)).start()
    mock.patch(
        'git_cl.Changelist.RunHook',
        return_value={'more_cc': ['test-more-cc@chromium.org']}).start()
    mock.patch('git_cl.watchlists.Watchlists', WatchlistsMock).start()
    mock.patch('git_cl.auth.Authenticator', AuthenticatorMock).start()
    mock.patch('gerrit_util.GetChangeDetail').start()
    mock.patch(
        'git_cl.gerrit_util.GetChangeComments',
        lambda *a: self._mocked_call('GetChangeComments', *a)).start()
    mock.patch(
        'git_cl.gerrit_util.GetChangeRobotComments',
        lambda *a: self._mocked_call('GetChangeRobotComments', *a)).start()
    mock.patch(
        'git_cl.gerrit_util.AddReviewers',
        lambda *a: self._mocked_call('AddReviewers', *a)).start()
    mock.patch(
        'git_cl.gerrit_util.SetReview',
        lambda h, i, msg=None, labels=None, notify=None, ready=None: (
            self._mocked_call(
                'SetReview', h, i, msg, labels, notify, ready))).start()
    mock.patch(
        'git_cl.gerrit_util.LuciContextAuthenticator.is_luci',
        return_value=False).start()
    mock.patch(
        'git_cl.gerrit_util.GceAuthenticator.is_gce',
        return_value=False).start()
    mock.patch(
        'git_cl.gerrit_util.ValidAccounts',
        lambda *a: self._mocked_call('ValidAccounts', *a)).start()
    mock.patch('sys.exit', side_effect=SystemExitMock).start()
    mock.patch('git_cl.Settings.GetRoot', return_value='').start()
    self.mockGit = GitMocks()
    mock.patch('scm.GIT.GetBranchRef', self.mockGit.GetBranchRef).start()
    mock.patch('scm.GIT.GetConfig', self.mockGit.GetConfig).start()
    mock.patch('scm.GIT.ResolveCommit', return_value='hash').start()
    mock.patch('scm.GIT.IsValidRevision', return_value=True).start()
    mock.patch('scm.GIT.SetConfig', self.mockGit.SetConfig).start()
    mock.patch(
        'git_new_branch.create_new_branch', self.mockGit.NewBranch).start()
    mock.patch(
        'scm.GIT.FetchUpstreamTuple',
        return_value=('origin', 'refs/heads/master')).start()
    mock.patch(
        'scm.GIT.CaptureStatus', return_value=[('M', 'foo.txt')]).start()
    # It's important to reset settings to not have inter-tests interference.
    git_cl.settings = None
    self.addCleanup(mock.patch.stopall)

  def tearDown(self):
    try:
      if not self.failed:
        self.assertEqual([], self.calls)
    except AssertionError:
      calls = ''.join('  %s\n' % str(call) for call in self.calls[:5])
      if len(self.calls) > 5:
        calls += ' ...\n'
      self.fail(
          '\n'
          'There are un-consumed calls after this test has finished:\n' +
          calls)
    finally:
      super(TestGitCl, self).tearDown()

  def _mocked_call(self, *args, **_kwargs):
    self.assertTrue(
        self.calls,
        '@%d  Expected: <Missing>   Actual: %r' % (len(self._calls_done), args))
    top = self.calls.pop(0)
    expected_args, result = top

    # Also logs otherwise it could get caught in a try/finally and be hard to
    # diagnose.
    if expected_args != args:
      N = 5
      prior_calls = '\n  '.join(
          '@%d: %r' % (len(self._calls_done) - N + i, c[0])
          for i, c in enumerate(self._calls_done[-N:]))
      following_calls = '\n  '.join(
          '@%d: %r' % (len(self._calls_done) + i + 1, c[0])
          for i, c in enumerate(self.calls[:N]))
      extended_msg = (
          'A few prior calls:\n  %s\n\n'
          'This (expected):\n  @%d: %r\n'
          'This (actual):\n  @%d: %r\n\n'
          'A few following expected calls:\n  %s' %
          (prior_calls, len(self._calls_done), expected_args,
           len(self._calls_done), args, following_calls))

      self.failed = True
      self.fail('@%d\n'
                '  Expected: %r\n'
                '  Actual:   %r\n'
                '\n'
                '%s' % (
          len(self._calls_done), expected_args, args, extended_msg))

    self._calls_done.append(top)
    if isinstance(result, Exception):
      raise result
    # stdout from git commands is supposed to be a bytestream. Convert it here
    # instead of converting all test output in this file to bytes.
    if args[0][0] == 'git' and not isinstance(result, bytes):
      result = result.encode('utf-8')
    return result

  @mock.patch('sys.stdin', StringIO('blah\nye\n'))
  @mock.patch('sys.stdout', StringIO())
  def test_ask_for_explicit_yes_true(self):
    self.assertTrue(git_cl.ask_for_explicit_yes('prompt'))
    self.assertEqual(
        'prompt [Yes/No]: Please, type yes or no: ',
        sys.stdout.getvalue())

  def test_LoadCodereviewSettingsFromFile_gerrit(self):
    codereview_file = StringIO('GERRIT_HOST: true')
    self.calls = [
      ((['git', 'config', '--unset-all', 'rietveld.cc'],), CERR1),
      ((['git', 'config', '--unset-all', 'rietveld.tree-status-url'],), CERR1),
      ((['git', 'config', '--unset-all', 'rietveld.viewvc-url'],), CERR1),
      ((['git', 'config', '--unset-all', 'rietveld.bug-prefix'],), CERR1),
      ((['git', 'config', '--unset-all', 'rietveld.cpplint-regex'],), CERR1),
      ((['git', 'config', '--unset-all', 'rietveld.cpplint-ignore-regex'],),
        CERR1),
      ((['git', 'config', '--unset-all', 'rietveld.run-post-upload-hook'],),
        CERR1),
      ((['git', 'config', '--unset-all', 'rietveld.format-full-by-default'],),
        CERR1),
      ((['git', 'config', 'gerrit.host', 'true'],), ''),
    ]
    self.assertIsNone(git_cl.LoadCodereviewSettingsFromFile(codereview_file))

  @classmethod
  def _gerrit_base_calls(cls, issue=None, fetched_description=None,
                         fetched_status=None, other_cl_owner=None,
                         custom_cl_base=None, short_hostname='chromium',
                         change_id=None):
    calls = []
    if custom_cl_base:
      ancestor_revision = custom_cl_base
    else:
      # Determine ancestor_revision to be merge base.
      ancestor_revision = 'fake_ancestor_sha'
      calls += [
        (('get_or_create_merge_base', 'master', 'refs/remotes/origin/master'),
         ancestor_revision),
      ]

    if issue:
      gerrit_util.GetChangeDetail.return_value = {
        'owner': {'email': (other_cl_owner or 'owner@example.com')},
        'change_id': (change_id or '123456789'),
        'current_revision': 'sha1_of_current_revision',
        'revisions': {'sha1_of_current_revision': {
          'commit': {'message': fetched_description},
        }},
        'status': fetched_status or 'NEW',
      }
      if fetched_status == 'ABANDONED':
        return calls
      if other_cl_owner:
        calls += [
          (('ask_for_data', 'Press Enter to upload, or Ctrl+C to abort'), ''),
        ]

    calls += [
      ((['git', 'diff', '--no-ext-diff', '--stat', '-l100000', '-C50'] +
         ([custom_cl_base] if custom_cl_base else
          [ancestor_revision, 'HEAD']),),
       '+dat'),
    ]

    return calls

  def _gerrit_upload_calls(self, description, reviewers, squash,
                           squash_mode='default',
                           title=None, notify=False,
                           post_amend_description=None, issue=None, cc=None,
                           custom_cl_base=None, tbr=None,
                           short_hostname='chromium',
                           labels=None, change_id=None,
                           final_description=None, gitcookies_exists=True,
                           force=False, edit_description=None):
    if post_amend_description is None:
      post_amend_description = description
    cc = cc or []

    calls = []

    if squash_mode in ('override_squash', 'override_nosquash'):
      self.mockGit.config['gerrit.override-squash-uploads'] = (
          'true' if squash_mode == 'override_squash' else 'false')

    if not git_footers.get_footer_change_id(description) and not squash:
      calls += [
        (('DownloadGerritHook', False), ''),
      ]
    if squash:
      if not issue and not force:
        calls += [
          ((['RunEditor'],), description),
        ]
      # user wants to edit description
      if edit_description:
        calls += [
          ((['RunEditor'],), edit_description),
        ]
      ref_to_push = 'abcdef0123456789'

      if custom_cl_base is None:
        calls += [
          (('get_or_create_merge_base', 'master', 'refs/remotes/origin/master'),
           'origin/master'),
        ]
        parent = 'origin/master'
      else:
        calls += [
          ((['git', 'merge-base', '--is-ancestor', custom_cl_base,
             'refs/remotes/origin/master'],),
           callError(1)),   # Means not ancenstor.
          (('ask_for_data',
            'Do you take responsibility for cleaning up potential mess '
            'resulting from proceeding with upload? Press Enter to upload, '
            'or Ctrl+C to abort'), ''),
        ]
        parent = custom_cl_base

      calls += [
        ((['git', 'rev-parse', 'HEAD:'],),  # `HEAD:` means HEAD's tree hash.
         '0123456789abcdef'),
        ((['FileWrite', '/tmp/fake-temp1', description],), None),
        ((['git', 'commit-tree', '0123456789abcdef', '-p', parent,
           '-F', '/tmp/fake-temp1'],),
         ref_to_push),
      ]
    else:
      ref_to_push = 'HEAD'
      parent = 'origin/refs/heads/master'

    calls += [
      (('SaveDescriptionBackup',), None),
      ((['git', 'rev-list', parent + '..' + ref_to_push],),'1hashPerLine\n'),
    ]

    metrics_arguments = []

    if notify:
      ref_suffix = '%ready,notify=ALL'
      metrics_arguments += ['ready', 'notify=ALL']
    else:
      if not issue and squash:
        ref_suffix = '%wip'
        metrics_arguments.append('wip')
      else:
        ref_suffix = '%notify=NONE'
        metrics_arguments.append('notify=NONE')

    # If issue is given, then description is fetched from Gerrit instead.
    if issue is None:
      if squash:
        title = 'Initial upload'
    else:
      if not title:
        calls += [
          ((['git', 'show', '-s', '--format=%s', 'HEAD'],), ''),
          (('ask_for_data', 'Title for patchset []: '), 'User input'),
        ]
        title = 'User input'
    if title:
      ref_suffix += ',m=' + gerrit_util.PercentEncodeForGitRef(title)
      metrics_arguments.append('m')

    if short_hostname == 'chromium':
      # All reviewers and ccs get into ref_suffix.
      for r in sorted(reviewers):
        ref_suffix += ',r=%s' % r
        metrics_arguments.append('r')
      if issue is None:
        cc += ['test-more-cc@chromium.org', 'joe@example.com']
      for c in sorted(cc):
        ref_suffix += ',cc=%s' % c
        metrics_arguments.append('cc')
      reviewers, cc = [], []
    else:
      # TODO(crbug/877717): remove this case.
      calls += [
        (('ValidAccounts', '%s-review.googlesource.com' % short_hostname,
          sorted(reviewers) + ['joe@example.com',
          'test-more-cc@chromium.org'] + cc),
         {
           e: {'email': e}
           for e in (reviewers + ['joe@example.com'] + cc)
         })
      ]
      for r in sorted(reviewers):
        if r != 'bad-account-or-email':
          ref_suffix  += ',r=%s' % r
          metrics_arguments.append('r')
          reviewers.remove(r)
      if issue is None:
        cc += ['joe@example.com']
      for c in sorted(cc):
        ref_suffix += ',cc=%s' % c
        metrics_arguments.append('cc')
        if c in cc:
          cc.remove(c)

    for k, v in sorted((labels or {}).items()):
      ref_suffix += ',l=%s+%d' % (k, v)
      metrics_arguments.append('l=%s+%d' % (k, v))

    if tbr:
      calls += [
        (('GetCodeReviewTbrScore',
          '%s-review.googlesource.com' % short_hostname,
          'my/repo'),
         2,),
      ]

    calls += [
      (('time.time',), 1000,),
      ((['git', 'push',
         'https://%s.googlesource.com/my/repo' % short_hostname,
         ref_to_push + ':refs/for/refs/heads/master' + ref_suffix],),
       (('remote:\n'
         'remote: Processing changes: (\)\n'
         'remote: Processing changes: (|)\n'
         'remote: Processing changes: (/)\n'
         'remote: Processing changes: (-)\n'
         'remote: Processing changes: new: 1 (/)\n'
         'remote: Processing changes: new: 1, done\n'
         'remote:\n'
         'remote: New Changes:\n'
         'remote:   https://%s-review.googlesource.com/#/c/my/repo/+/123456'
             ' XXX\n'
         'remote:\n'
         'To https://%s.googlesource.com/my/repo\n'
         ' * [new branch]      hhhh -> refs/for/refs/heads/master\n'
         ) % (short_hostname, short_hostname)),),
      (('time.time',), 2000,),
      (('add_repeated',
        'sub_commands',
        {
          'execution_time': 1000,
          'command': 'git push',
          'exit_code': 0,
          'arguments': sorted(metrics_arguments),
        }),
        None,),
    ]

    final_description = final_description or post_amend_description.strip()

    trace_name = os.path.join('TRACES_DIR', '20170316T200041.000000')

    # Trace-related calls
    calls += [
        # Write a description with context for the current trace.
        (
            ([
                'FileWrite', trace_name + '-README',
                '%(date)s\n'
                '%(short_hostname)s-review.googlesource.com\n'
                '%(change_id)s\n'
                '%(title)s\n'
                '%(description)s\n'
                '1000\n'
                '0\n'
                '%(trace_name)s' % {
                    'date': '2017-03-16T20:00:41.000000',
                    'short_hostname': short_hostname,
                    'change_id': change_id,
                    'description': final_description,
                    'title': title or '<untitled>',
                    'trace_name': trace_name,
                }
            ], ),
            None,
        ),
        # Read traces and shorten git hashes.
        (
            (['os.path.isfile',
              os.path.join('TEMP_DIR', 'trace-packet')], ),
            True,
        ),
        (
            (['FileRead', os.path.join('TEMP_DIR', 'trace-packet')], ),
            ('git-hash: 0123456789012345678901234567890123456789\n'
             'git-hash: abcdeabcdeabcdeabcdeabcdeabcdeabcdeabcde\n'),
        ),
        (
            ([
                'FileWrite',
                os.path.join('TEMP_DIR', 'trace-packet'), 'git-hash: 012345\n'
                'git-hash: abcdea\n'
            ], ),
            None,
        ),
        # Make zip file for the git traces.
        (
            (['make_archive', trace_name + '-traces', 'zip', 'TEMP_DIR'], ),
            None,
        ),
        # Collect git config and gitcookies.
        (
            (['git', 'config', '-l'], ),
            'git-config-output',
        ),
        (
            ([
                'FileWrite',
                os.path.join('TEMP_DIR', 'git-config'), 'git-config-output'
            ], ),
            None,
        ),
        (
            (['os.path.isfile',
              os.path.join('~', '.gitcookies')], ),
            gitcookies_exists,
        ),
    ]
    if gitcookies_exists:
      calls += [
          (
              (['FileRead', os.path.join('~', '.gitcookies')], ),
              'gitcookies 1/SECRET',
          ),
          (
              ([
                  'FileWrite',
                  os.path.join('TEMP_DIR', 'gitcookies'), 'gitcookies REDACTED'
              ], ),
              None,
          ),
      ]
    calls += [
        # Make zip file for the git config and gitcookies.
        (
            (['make_archive', trace_name + '-git-info', 'zip', 'TEMP_DIR'], ),
            None,
        ),
    ]

    # TODO(crbug/877717): this should never be used.
    if squash and short_hostname != 'chromium':
      calls += [
          (('AddReviewers',
            'chromium-review.googlesource.com', 'my%2Frepo~123456',
            sorted(reviewers),
            cc + ['test-more-cc@chromium.org'],
            notify),
           ''),
      ]
    return calls

  def _run_gerrit_upload_test(
      self,
      upload_args,
      description,
      reviewers=None,
      squash=True,
      squash_mode=None,
      title=None,
      notify=False,
      post_amend_description=None,
      issue=None,
      cc=None,
      fetched_status=None,
      other_cl_owner=None,
      custom_cl_base=None,
      tbr=None,
      short_hostname='chromium',
      labels=None,
      change_id=None,
      final_description=None,
      gitcookies_exists=True,
      force=False,
      log_description=None,
      edit_description=None,
      fetched_description=None):
    """Generic gerrit upload test framework."""
    if squash_mode is None:
      if '--no-squash' in upload_args:
        squash_mode = 'nosquash'
      elif '--squash' in upload_args:
        squash_mode = 'squash'
      else:
        squash_mode = 'default'

    reviewers = reviewers or []
    cc = cc or []
    mock.patch('git_cl.gerrit_util.CookiesAuthenticator',
              CookiesAuthenticatorMockFactory(
                same_auth=('git-owner.example.com', '', 'pass'))).start()
    mock.patch('git_cl.Changelist._GerritCommitMsgHookCheck',
              lambda _, offer_removal: None).start()
    mock.patch('git_cl.gclient_utils.RunEditor',
              lambda *_, **__: self._mocked_call(['RunEditor'])).start()
    mock.patch('git_cl.DownloadGerritHook', lambda force: self._mocked_call(
               'DownloadGerritHook', force)).start()
    mock.patch('git_cl.gclient_utils.FileRead',
              lambda path: self._mocked_call(['FileRead', path])).start()
    mock.patch('git_cl.gclient_utils.FileWrite',
              lambda path, contents: self._mocked_call(
                  ['FileWrite', path, contents])).start()
    mock.patch('git_cl.datetime_now',
             lambda: datetime.datetime(2017, 3, 16, 20, 0, 41, 0)).start()
    mock.patch('git_cl.tempfile.mkdtemp', lambda: 'TEMP_DIR').start()
    mock.patch('git_cl.TRACES_DIR', 'TRACES_DIR').start()
    mock.patch('git_cl.TRACES_README_FORMAT',
              '%(now)s\n'
              '%(gerrit_host)s\n'
              '%(change_id)s\n'
              '%(title)s\n'
              '%(description)s\n'
              '%(execution_time)s\n'
              '%(exit_code)s\n'
              '%(trace_name)s').start()
    mock.patch('git_cl.shutil.make_archive',
              lambda *args: self._mocked_call(['make_archive'] +
              list(args))).start()
    mock.patch('os.path.isfile',
              lambda path: self._mocked_call(['os.path.isfile', path])).start()
    mock.patch(
        'git_cl._create_description_from_log',
        return_value=log_description or description).start()
    mock.patch(
        'git_cl.Changelist._AddChangeIdToCommitMessage',
        return_value=post_amend_description or description).start()
    mock.patch(
        'git_cl.GenerateGerritChangeId', return_value=change_id).start()
    mock.patch(
        'gclient_utils.AskForData',
        lambda prompt: self._mocked_call('ask_for_data', prompt)).start()

    self.mockGit.config['gerrit.host'] = 'true'
    self.mockGit.config['branch.master.gerritissue'] = (
        str(issue) if issue else None)
    self.mockGit.config['remote.origin.url'] = (
        'https://%s.googlesource.com/my/repo' % short_hostname)
    self.mockGit.config['user.email'] = 'me@example.com'

    self.calls = self._gerrit_base_calls(
        issue=issue,
        fetched_description=fetched_description or description,
        fetched_status=fetched_status,
        other_cl_owner=other_cl_owner,
        custom_cl_base=custom_cl_base,
        short_hostname=short_hostname,
        change_id=change_id)
    if fetched_status != 'ABANDONED':
      mock.patch(
          'gclient_utils.temporary_file', TemporaryFileMock()).start()
      mock.patch('os.remove', return_value=True).start()
      self.calls += self._gerrit_upload_calls(
          description, reviewers, squash,
          squash_mode=squash_mode,
          title=title, notify=notify,
          post_amend_description=post_amend_description,
          issue=issue, cc=cc,
          custom_cl_base=custom_cl_base, tbr=tbr,
          short_hostname=short_hostname,
          labels=labels,
          change_id=change_id,
          final_description=final_description,
          gitcookies_exists=gitcookies_exists,
          force=force,
          edit_description=edit_description)
    # Uncomment when debugging.
    # print('\n'.join(map(lambda x: '%2i: %s' % x, enumerate(self.calls))))
    git_cl.main(['upload'] + upload_args)
    if squash:
      self.assertIssueAndPatchset(patchset=None)
      self.assertEqual(
          'abcdef0123456789',
          scm.GIT.GetBranchConfig('', 'master', 'gerritsquashhash'))

  def test_gerrit_upload_traces_no_gitcookies(self):
    self._run_gerrit_upload_test(
        ['--no-squash'],
        'desc ✔\n\nBUG=\n',
        [],
        squash=False,
        post_amend_description='desc ✔\n\nBUG=\n\nChange-Id: Ixxx',
        change_id='Ixxx',
        gitcookies_exists=False)

  def test_gerrit_upload_without_change_id(self):
    self._run_gerrit_upload_test(
        [],
        'desc ✔\n\nBUG=\n\nChange-Id: Ixxx',
        [],
        change_id='Ixxx')

  def test_gerrit_upload_without_change_id_nosquash(self):
    self._run_gerrit_upload_test(
        ['--no-squash'],
        'desc ✔\n\nBUG=\n',
        [],
        squash=False,
        post_amend_description='desc ✔\n\nBUG=\n\nChange-Id: Ixxx',
        change_id='Ixxx')

  def test_gerrit_upload_without_change_id_override_nosquash(self):
    self._run_gerrit_upload_test(
        [],
        'desc ✔\n\nBUG=\n',
        [],
        squash=False,
        squash_mode='override_nosquash',
        post_amend_description='desc ✔\n\nBUG=\n\nChange-Id: Ixxx',
        change_id='Ixxx')

  def test_gerrit_no_reviewer(self):
    self._run_gerrit_upload_test(
        [],
        'desc ✔\n\nBUG=\n\nChange-Id: I123456789\n',
        [],
        squash=False,
        squash_mode='override_nosquash',
        change_id='I123456789')

  def test_gerrit_no_reviewer_non_chromium_host(self):
    # TODO(crbug/877717): remove this test case.
    self._run_gerrit_upload_test(
        [],
        'desc ✔\n\nBUG=\n\nChange-Id: I123456789\n',
        [],
        squash=False,
        squash_mode='override_nosquash',
        short_hostname='other',
        change_id='I123456789')

  def test_gerrit_patchset_title_special_chars_nosquash(self):
    self._run_gerrit_upload_test(
        ['-f', '-t', 'We\'ll escape ^_ ^ special chars...@{u}'],
        'desc ✔\n\nBUG=\n\nChange-Id: I123456789',
        squash=False,
        squash_mode='override_nosquash',
        change_id='I123456789',
        title='We\'ll escape ^_ ^ special chars...@{u}')

  def test_gerrit_reviewers_cmd_line(self):
    self._run_gerrit_upload_test(
        ['-r', 'foo@example.com', '--send-mail'],
        'desc ✔\n\nBUG=\n\nChange-Id: I123456789',
        reviewers=['foo@example.com'],
        squash=False,
        squash_mode='override_nosquash',
        notify=True,
        change_id='I123456789',
        final_description=(
            'desc ✔\n\nBUG=\nR=foo@example.com\n\nChange-Id: I123456789'))

  def test_gerrit_upload_force_sets_bug(self):
    self._run_gerrit_upload_test(
        ['-b', '10000', '-f'],
        u'desc=\n\nBug: 10000\nChange-Id: Ixxx',
        [],
        force=True,
        fetched_description='desc=\n\nChange-Id: Ixxx',
        change_id='Ixxx')

  def test_gerrit_upload_corrects_wrong_change_id(self):
    self._run_gerrit_upload_test(
        ['-b', '10000', '-m', 'Title', '--edit-description'],
        u'desc=\n\nBug: 10000\nChange-Id: Ixxxx',
        [],
        issue='123456',
        edit_description='desc=\n\nBug: 10000\nChange-Id: Izzzz',
        fetched_description='desc=\n\nChange-Id: Ixxxx',
        title='Title',
        change_id='Ixxxx')

  def test_gerrit_upload_force_sets_fixed(self):
    self._run_gerrit_upload_test(
        ['-x', '10000', '-f'],
        u'desc=\n\nFixed: 10000\nChange-Id: Ixxx',
        [],
        force=True,
        fetched_description='desc=\n\nChange-Id: Ixxx',
        change_id='Ixxx')

  def test_gerrit_reviewer_multiple(self):
    mock.patch('git_cl.gerrit_util.GetCodeReviewTbrScore',
              lambda *a: self._mocked_call('GetCodeReviewTbrScore', *a)).start()
    self._run_gerrit_upload_test(
        [],
        'desc ✔\nTBR=reviewer@example.com\nBUG=\nR=another@example.com\n'
        'CC=more@example.com,people@example.com\n\n'
        'Change-Id: 123456789',
        ['reviewer@example.com', 'another@example.com'],
        cc=['more@example.com', 'people@example.com'],
        tbr='reviewer@example.com',
        labels={'Code-Review': 2},
        change_id='123456789')

  def test_gerrit_upload_squash_first_is_default(self):
    self._run_gerrit_upload_test(
        [],
        'desc ✔\nBUG=\n\nChange-Id: 123456789',
        [],
        change_id='123456789')

  def test_gerrit_upload_squash_first(self):
    self._run_gerrit_upload_test(
        ['--squash'],
        'desc ✔\nBUG=\n\nChange-Id: 123456789',
        [],
        squash=True,
        change_id='123456789')

  def test_gerrit_upload_squash_first_title(self):
    self._run_gerrit_upload_test(
        ['-f', '-t', 'title'],
        'title\n\ndesc\n\nChange-Id: 123456789',
        [],
        force=True,
        squash=True,
        log_description='desc',
        change_id='123456789')

  def test_gerrit_upload_squash_first_with_labels(self):
    self._run_gerrit_upload_test(
        ['--squash', '--cq-dry-run', '--enable-auto-submit'],
        'desc ✔\nBUG=\n\nChange-Id: 123456789',
        [],
        squash=True,
        labels={'Commit-Queue': 1, 'Auto-Submit': 1},
        change_id='123456789')

  def test_gerrit_upload_squash_first_against_rev(self):
    custom_cl_base = 'custom_cl_base_rev_or_branch'
    self._run_gerrit_upload_test(
        ['--squash', custom_cl_base],
        'desc ✔\nBUG=\n\nChange-Id: 123456789',
        [],
        squash=True,
        custom_cl_base=custom_cl_base,
        change_id='123456789')
    self.assertIn(
        'If you proceed with upload, more than 1 CL may be created by Gerrit',
        sys.stdout.getvalue())

  def test_gerrit_upload_squash_reupload(self):
    description = 'desc ✔\nBUG=\n\nChange-Id: 123456789'
    self._run_gerrit_upload_test(
        ['--squash'],
        description,
        [],
        squash=True,
        issue=123456,
        change_id='123456789')

  @mock.patch('sys.stderr', StringIO())
  def test_gerrit_upload_squash_reupload_to_abandoned(self):
    description = 'desc ✔\nBUG=\n\nChange-Id: 123456789'
    with self.assertRaises(SystemExitMock):
      self._run_gerrit_upload_test(
          ['--squash'],
          description,
          [],
          squash=True,
          issue=123456,
          fetched_status='ABANDONED',
          change_id='123456789')
    self.assertEqual(
        'Change https://chromium-review.googlesource.com/123456 has been '
        'abandoned, new uploads are not allowed\n',
        sys.stderr.getvalue())

  @mock.patch(
      'gerrit_util.GetAccountDetails',
      return_value={'email': 'yet-another@example.com'})
  def test_gerrit_upload_squash_reupload_to_not_owned(self, _mock):
    description = 'desc ✔\nBUG=\n\nChange-Id: 123456789'
    self._run_gerrit_upload_test(
          ['--squash'],
          description,
          [],
          squash=True,
          issue=123456,
          other_cl_owner='other@example.com',
          change_id='123456789')
    self.assertIn(
        'WARNING: Change 123456 is owned by other@example.com, but you '
        'authenticate to Gerrit as yet-another@example.com.\n'
        'Uploading may fail due to lack of permissions',
        sys.stdout.getvalue())

  def test_upload_change_description_editor(self):
    fetched_description = 'foo\n\nChange-Id: 123456789'
    description = 'bar\n\nChange-Id: 123456789'
    self._run_gerrit_upload_test(
        ['--squash', '--edit-description'],
        description,
        [],
        fetched_description=fetched_description,
        squash=True,
        issue=123456,
        change_id='123456789',
        edit_description=description)

  @mock.patch('git_cl.RunGit')
  @mock.patch('git_cl.CMDupload')
  @mock.patch('sys.stdin', StringIO('\n'))
  @mock.patch('sys.stdout', StringIO())
  def test_upload_branch_deps(self, *_mocks):
    def mock_run_git(*args, **_kwargs):
      if args[0] == ['for-each-ref',
                       '--format=%(refname:short) %(upstream:short)',
                       'refs/heads']:
        # Create a local branch dependency tree that looks like this:
        # test1 -> test2 -> test3   -> test4 -> test5
        #                -> test3.1
        # test6 -> test0
        branch_deps = [
            'test2 test1',    # test1 -> test2
            'test3 test2',    # test2 -> test3
            'test3.1 test2',  # test2 -> test3.1
            'test4 test3',    # test3 -> test4
            'test5 test4',    # test4 -> test5
            'test6 test0',    # test0 -> test6
            'test7',          # test7
        ]
        return '\n'.join(branch_deps)
    git_cl.RunGit.side_effect = mock_run_git
    git_cl.CMDupload.return_value = 0

    class MockChangelist():
      def __init__(self):
        pass
      def GetBranch(self):
        return 'test1'
      def GetIssue(self):
        return '123'
      def GetPatchset(self):
        return '1001'
      def IsGerrit(self):
        return False

    ret = git_cl.upload_branch_deps(MockChangelist(), [])
    # CMDupload should have been called 5 times because of 5 dependent branches.
    self.assertEqual(5, len(git_cl.CMDupload.mock_calls))
    self.assertIn(
        'This command will checkout all dependent branches '
        'and run "git cl upload". Press Enter to continue, '
        'or Ctrl+C to abort',
        sys.stdout.getvalue())
    self.assertEqual(0, ret)

  def test_gerrit_change_id(self):
    self.calls = [
        ((['git', 'write-tree'], ),
          'hashtree'),
        ((['git', 'rev-parse', 'HEAD~0'], ),
          'branch-parent'),
        ((['git', 'var', 'GIT_AUTHOR_IDENT'], ),
          'A B <a@b.org> 1456848326 +0100'),
        ((['git', 'var', 'GIT_COMMITTER_IDENT'], ),
          'C D <c@d.org> 1456858326 +0100'),
        ((['git', 'hash-object', '-t', 'commit', '--stdin'], ),
          'hashchange'),
    ]
    change_id = git_cl.GenerateGerritChangeId('line1\nline2\n')
    self.assertEqual(change_id, 'Ihashchange')

  def test_desecription_append_footer(self):
    for init_desc, footer_line, expected_desc in [
      # Use unique desc first lines for easy test failure identification.
      ('foo', 'R=one', 'foo\n\nR=one'),
      ('foo\n\nR=one', 'BUG=', 'foo\n\nR=one\nBUG='),
      ('foo\n\nR=one', 'Change-Id: Ixx', 'foo\n\nR=one\n\nChange-Id: Ixx'),
      ('foo\n\nChange-Id: Ixx', 'R=one', 'foo\n\nR=one\n\nChange-Id: Ixx'),
      ('foo\n\nR=one\n\nChange-Id: Ixx', 'TBR=two',
       'foo\n\nR=one\nTBR=two\n\nChange-Id: Ixx'),
      ('foo\n\nR=one\n\nChange-Id: Ixx', 'Foo-Bar: baz',
       'foo\n\nR=one\n\nChange-Id: Ixx\nFoo-Bar: baz'),
      ('foo\n\nChange-Id: Ixx', 'Foo-Bak: baz',
       'foo\n\nChange-Id: Ixx\nFoo-Bak: baz'),
      ('foo', 'Change-Id: Ixx', 'foo\n\nChange-Id: Ixx'),
    ]:
      desc = git_cl.ChangeDescription(init_desc)
      desc.append_footer(footer_line)
      self.assertEqual(desc.description, expected_desc)

  def test_update_reviewers(self):
    data = [
      ('foo', [], [],
       'foo'),
      ('foo\nR=xx', [], [],
       'foo\nR=xx'),
      ('foo\nTBR=xx', [], [],
       'foo\nTBR=xx'),
      ('foo', ['a@c'], [],
       'foo\n\nR=a@c'),
      ('foo\nR=xx', ['a@c'], [],
       'foo\n\nR=a@c, xx'),
      ('foo\nTBR=xx', ['a@c'], [],
       'foo\n\nR=a@c\nTBR=xx'),
      ('foo\nTBR=xx\nR=yy', ['a@c'], [],
       'foo\n\nR=a@c, yy\nTBR=xx'),
      ('foo\nBUG=', ['a@c'], [],
       'foo\nBUG=\nR=a@c'),
      ('foo\nR=xx\nTBR=yy\nR=bar', ['a@c'], [],
       'foo\n\nR=a@c, bar, xx\nTBR=yy'),
      ('foo', ['a@c', 'b@c'], [],
       'foo\n\nR=a@c, b@c'),
      ('foo\nBar\n\nR=\nBUG=', ['c@c'], [],
       'foo\nBar\n\nR=c@c\nBUG='),
      ('foo\nBar\n\nR=\nBUG=\nR=', ['c@c'], [],
       'foo\nBar\n\nR=c@c\nBUG='),
      # Same as the line before, but full of whitespaces.
      (
        'foo\nBar\n\n R = \n BUG = \n R = ', ['c@c'], [],
        'foo\nBar\n\nR=c@c\n BUG =',
      ),
      # Whitespaces aren't interpreted as new lines.
      ('foo BUG=allo R=joe ', ['c@c'], [],
       'foo BUG=allo R=joe\n\nR=c@c'),
      # Redundant TBRs get promoted to Rs
      ('foo\n\nR=a@c\nTBR=t@c', ['b@c', 'a@c'], ['a@c', 't@c'],
       'foo\n\nR=a@c, b@c\nTBR=t@c'),
    ]
    expected = [i[-1] for i in data]
    actual = []
    for orig, reviewers, tbrs, _expected in data:
      obj = git_cl.ChangeDescription(orig)
      obj.update_reviewers(reviewers, tbrs, None, None, None)
      actual.append(obj.description)
    self.assertEqual(expected, actual)

  def test_get_hash_tags(self):
    cases = [
      ('', []),
      ('a', []),
      ('[a]', ['a']),
      ('[aa]', ['aa']),
      ('[a ]', ['a']),
      ('[a- ]', ['a']),
      ('[a- b]', ['a-b']),
      ('[a--b]', ['a-b']),
      ('[a', []),
      ('[a]x', ['a']),
      ('[aa]x', ['aa']),
      ('[a b]', ['a-b']),
      ('[a  b]', ['a-b']),
      ('[a__b]', ['a-b']),
      ('[a] x', ['a']),
      ('[a][b]', ['a', 'b']),
      ('[a] [b]', ['a', 'b']),
      ('[a][b]x', ['a', 'b']),
      ('[a][b] x', ['a', 'b']),
      ('[a]\n[b]', ['a']),
      ('[a\nb]', []),
      ('[a][', ['a']),
      ('Revert "[a] feature"', ['a']),
      ('Reland "[a] feature"', ['a']),
      ('Revert: [a] feature', ['a']),
      ('Reland: [a] feature', ['a']),
      ('Revert "Reland: [a] feature"', ['a']),
      ('Foo: feature', ['foo']),
      ('Foo Bar: feature', ['foo-bar']),
      ('Change Foo::Bar', []),
      ('Foo: Change Foo::Bar', ['foo']),
      ('Revert "Foo bar: feature"', ['foo-bar']),
      ('Reland "Foo bar: feature"', ['foo-bar']),
    ]
    for desc, expected in cases:
      change_desc = git_cl.ChangeDescription(desc)
      actual = change_desc.get_hash_tags()
      self.assertEqual(
          actual,
          expected,
          'GetHashTags(%r) == %r, expected %r' % (desc, actual, expected))

    self.assertEqual(None, git_cl.GetTargetRef('origin', None, 'master'))
    self.assertEqual(None, git_cl.GetTargetRef(None,
                                               'refs/remotes/origin/master',
                                               'master'))

    # Check default target refs for branches.
    self.assertEqual('refs/heads/master',
                     git_cl.GetTargetRef('origin', 'refs/remotes/origin/master',
                                         None))
    self.assertEqual('refs/heads/master',
                     git_cl.GetTargetRef('origin', 'refs/remotes/origin/lkgr',
                                         None))
    self.assertEqual('refs/heads/master',
                     git_cl.GetTargetRef('origin', 'refs/remotes/origin/lkcr',
                                         None))
    self.assertEqual('refs/branch-heads/123',
                     git_cl.GetTargetRef('origin',
                                         'refs/remotes/branch-heads/123',
                                         None))
    self.assertEqual('refs/diff/test',
                     git_cl.GetTargetRef('origin',
                                         'refs/remotes/origin/refs/diff/test',
                                         None))
    self.assertEqual('refs/heads/chrome/m42',
                     git_cl.GetTargetRef('origin',
                                         'refs/remotes/origin/chrome/m42',
                                         None))

    # Check target refs for user-specified target branch.
    for branch in ('branch-heads/123', 'remotes/branch-heads/123',
                   'refs/remotes/branch-heads/123'):
      self.assertEqual('refs/branch-heads/123',
                       git_cl.GetTargetRef('origin',
                                           'refs/remotes/origin/master',
                                           branch))
    for branch in ('origin/master', 'remotes/origin/master',
                   'refs/remotes/origin/master'):
      self.assertEqual('refs/heads/master',
                       git_cl.GetTargetRef('origin',
                                           'refs/remotes/branch-heads/123',
                                           branch))
    for branch in ('master', 'heads/master', 'refs/heads/master'):
      self.assertEqual('refs/heads/master',
                       git_cl.GetTargetRef('origin',
                                           'refs/remotes/branch-heads/123',
                                           branch))

  @mock.patch('git_common.is_dirty_git_tree', return_value=True)
  def test_patch_when_dirty(self, *_mocks):
    # Patch when local tree is dirty.
    self.assertNotEqual(git_cl.main(['patch', '123456']), 0)

  def assertIssueAndPatchset(
      self, branch='master', issue='123456', patchset='7',
      git_short_host='chromium'):
    self.assertEqual(
        issue, scm.GIT.GetBranchConfig('', branch, 'gerritissue'))
    self.assertEqual(
        patchset, scm.GIT.GetBranchConfig('', branch, 'gerritpatchset'))
    self.assertEqual(
        'https://%s-review.googlesource.com' % git_short_host,
        scm.GIT.GetBranchConfig('', branch, 'gerritserver'))

  def _patch_common(self, git_short_host='chromium'):
    mock.patch('scm.GIT.ResolveCommit', return_value='deadbeef').start()
    self.mockGit.config['remote.origin.url'] = (
        'https://%s.googlesource.com/my/repo' % git_short_host)
    gerrit_util.GetChangeDetail.return_value = {
      'current_revision': '7777777777',
      'revisions': {
        '1111111111': {
          '_number': 1,
          'fetch': {'http': {
            'url': 'https://%s.googlesource.com/my/repo' % git_short_host,
            'ref': 'refs/changes/56/123456/1',
          }},
        },
        '7777777777': {
          '_number': 7,
          'fetch': {'http': {
            'url': 'https://%s.googlesource.com/my/repo' % git_short_host,
            'ref': 'refs/changes/56/123456/7',
          }},
        },
      },
    }

  def test_patch_gerrit_default(self):
    self._patch_common()
    self.calls += [
      ((['git', 'fetch', 'https://chromium.googlesource.com/my/repo',
         'refs/changes/56/123456/7'],), ''),
      ((['git', 'cherry-pick', 'FETCH_HEAD'],), ''),
    ]
    self.assertEqual(git_cl.main(['patch', '123456']), 0)
    self.assertIssueAndPatchset()

  def test_patch_gerrit_new_branch(self):
    self._patch_common()
    self.calls += [
      ((['git', 'fetch', 'https://chromium.googlesource.com/my/repo',
         'refs/changes/56/123456/7'],), ''),
      ((['git', 'cherry-pick', 'FETCH_HEAD'],), ''),
    ]
    self.assertEqual(git_cl.main(['patch', '-b', 'feature', '123456']), 0)
    self.assertIssueAndPatchset(branch='feature')

  def test_patch_gerrit_force(self):
    self._patch_common('host')
    self.calls += [
      ((['git', 'fetch', 'https://host.googlesource.com/my/repo',
         'refs/changes/56/123456/7'],), ''),
      ((['git', 'reset', '--hard', 'FETCH_HEAD'],), ''),
    ]
    self.assertEqual(git_cl.main(['patch', '123456', '--force']), 0)
    self.assertIssueAndPatchset(git_short_host='host')

  def test_patch_gerrit_guess_by_url(self):
    self._patch_common('else')
    self.calls += [
      ((['git', 'fetch', 'https://else.googlesource.com/my/repo',
         'refs/changes/56/123456/1'],), ''),
      ((['git', 'cherry-pick', 'FETCH_HEAD'],), ''),
    ]
    self.assertEqual(git_cl.main(
      ['patch', 'https://else-review.googlesource.com/#/c/123456/1']), 0)
    self.assertIssueAndPatchset(patchset='1', git_short_host='else')

  def test_patch_gerrit_guess_by_url_with_repo(self):
    self._patch_common('else')
    self.calls += [
      ((['git', 'fetch', 'https://else.googlesource.com/my/repo',
         'refs/changes/56/123456/1'],), ''),
      ((['git', 'cherry-pick', 'FETCH_HEAD'],), ''),
    ]
    self.assertEqual(git_cl.main(
      ['patch', 'https://else-review.googlesource.com/c/my/repo/+/123456/1']),
      0)
    self.assertIssueAndPatchset(patchset='1', git_short_host='else')

  @mock.patch('sys.stderr', StringIO())
  def test_patch_gerrit_conflict(self):
    self._patch_common()
    self.calls += [
      ((['git', 'fetch', 'https://chromium.googlesource.com/my/repo',
         'refs/changes/56/123456/7'],), ''),
      ((['git', 'cherry-pick', 'FETCH_HEAD'],), CERR1),
    ]
    with self.assertRaises(SystemExitMock):
      git_cl.main(['patch', '123456'])
    self.assertEqual(
        'Command "git cherry-pick FETCH_HEAD" failed.\n\n',
        sys.stderr.getvalue())

  @mock.patch(
      'gerrit_util.GetChangeDetail',
      side_effect=gerrit_util.GerritError(404, ''))
  @mock.patch('sys.stderr', StringIO())
  def test_patch_gerrit_not_exists(self, *_mocks):
    self.mockGit.config['remote.origin.url'] = (
        'https://chromium.googlesource.com/my/repo')
    with self.assertRaises(SystemExitMock):
      self.assertEqual(1, git_cl.main(['patch', '123456']))
    self.assertEqual(
        'change 123456 at https://chromium-review.googlesource.com does not '
        'exist or you have no access to it\n',
        sys.stderr.getvalue())

  def _checkout_calls(self):
    return [
        ((['git', 'config', '--local', '--get-regexp',
           'branch\\..*\\.gerritissue'], ),
           ('branch.ger-branch.gerritissue 123456\n'
            'branch.gbranch654.gerritissue 654321\n')),
    ]

  def test_checkout_gerrit(self):
    """Tests git cl checkout <issue>."""
    self.calls = self._checkout_calls()
    self.calls += [((['git', 'checkout', 'ger-branch'], ), '')]
    self.assertEqual(0, git_cl.main(['checkout', '123456']))

  def test_checkout_not_found(self):
    """Tests git cl checkout <issue>."""
    self.calls = self._checkout_calls()
    self.assertEqual(1, git_cl.main(['checkout', '99999']))

  def test_checkout_no_branch_issues(self):
    """Tests git cl checkout <issue>."""
    self.calls = [
        ((['git', 'config', '--local', '--get-regexp',
           'branch\\..*\\.gerritissue'], ), CERR1),
    ]
    self.assertEqual(1, git_cl.main(['checkout', '99999']))

  def _test_gerrit_ensure_authenticated_common(self, auth):
    mock.patch(
        'gclient_utils.AskForData',
        lambda prompt: self._mocked_call('ask_for_data', prompt)).start()
    mock.patch('git_cl.gerrit_util.CookiesAuthenticator',
              CookiesAuthenticatorMockFactory(hosts_with_creds=auth)).start()
    self.mockGit.config['remote.origin.url'] = (
        'https://chromium.googlesource.com/my/repo')
    cl = git_cl.Changelist()
    cl.branch = 'master'
    cl.branchref = 'refs/heads/master'
    return cl

  @mock.patch('sys.stderr', StringIO())
  def test_gerrit_ensure_authenticated_missing(self):
    cl = self._test_gerrit_ensure_authenticated_common(auth={
      'chromium.googlesource.com': ('git-is.ok', '', 'but gerrit is missing'),
    })
    with self.assertRaises(SystemExitMock):
      cl.EnsureAuthenticated(force=False)
    self.assertEqual(
        'Credentials for the following hosts are required:\n'
        '  chromium-review.googlesource.com\n'
        'These are read from ~%(sep)s.gitcookies '
        '(or legacy ~%(sep)s%(netrc)s)\n'
        'You can (re)generate your credentials by visiting '
        'https://chromium-review.googlesource.com/new-password\n' % {
            'sep': os.sep,
            'netrc': NETRC_FILENAME,
        }, sys.stderr.getvalue())

  def test_gerrit_ensure_authenticated_conflict(self):
    cl = self._test_gerrit_ensure_authenticated_common(auth={
      'chromium.googlesource.com':
          ('git-one.example.com', None, 'secret1'),
      'chromium-review.googlesource.com':
          ('git-other.example.com', None, 'secret2'),
    })
    self.calls.append(
        (('ask_for_data', 'If you know what you are doing '
                          'press Enter to continue, or Ctrl+C to abort'), ''))
    self.assertIsNone(cl.EnsureAuthenticated(force=False))

  def test_gerrit_ensure_authenticated_ok(self):
    cl = self._test_gerrit_ensure_authenticated_common(auth={
      'chromium.googlesource.com':
          ('git-same.example.com', None, 'secret'),
      'chromium-review.googlesource.com':
          ('git-same.example.com', None, 'secret'),
    })
    self.assertIsNone(cl.EnsureAuthenticated(force=False))

  def test_gerrit_ensure_authenticated_skipped(self):
    self.mockGit.config['gerrit.skip-ensure-authenticated'] = 'true'
    cl = self._test_gerrit_ensure_authenticated_common(auth={})
    self.assertIsNone(cl.EnsureAuthenticated(force=False))

  def test_gerrit_ensure_authenticated_bearer_token(self):
    cl = self._test_gerrit_ensure_authenticated_common(auth={
      'chromium.googlesource.com':
          ('', None, 'secret'),
      'chromium-review.googlesource.com':
          ('', None, 'secret'),
    })
    self.assertIsNone(cl.EnsureAuthenticated(force=False))
    header = gerrit_util.CookiesAuthenticator().get_auth_header(
        'chromium.googlesource.com')
    self.assertTrue('Bearer' in header)

  def test_gerrit_ensure_authenticated_non_https(self):
    self.mockGit.config['remote.origin.url'] = 'custom-scheme://repo'
    self.calls = [
        (('logging.warning',
            'Ignoring branch %(branch)s with non-https remote '
            '%(remote)s', {
              'branch': 'master',
              'remote': 'custom-scheme://repo'}
          ), None),
    ]
    mock.patch('git_cl.gerrit_util.CookiesAuthenticator',
              CookiesAuthenticatorMockFactory(hosts_with_creds={})).start()
    mock.patch('logging.warning',
              lambda *a: self._mocked_call('logging.warning', *a)).start()
    cl = git_cl.Changelist()
    cl.branch = 'master'
    cl.branchref = 'refs/heads/master'
    cl.lookedup_issue = True
    self.assertIsNone(cl.EnsureAuthenticated(force=False))

  def test_gerrit_ensure_authenticated_non_url(self):
    self.mockGit.config['remote.origin.url'] = (
        'git@somehost.example:foo/bar.git')
    self.calls = [
        (('logging.error',
            'Remote "%(remote)s" for branch "%(branch)s" points to "%(url)s", '
            'but it doesn\'t exist.', {
              'remote': 'origin',
              'branch': 'master',
              'url': 'git@somehost.example:foo/bar.git'}
          ), None),
    ]
    mock.patch('git_cl.gerrit_util.CookiesAuthenticator',
              CookiesAuthenticatorMockFactory(hosts_with_creds={})).start()
    mock.patch('logging.error',
              lambda *a: self._mocked_call('logging.error', *a)).start()
    cl = git_cl.Changelist()
    cl.branch = 'master'
    cl.branchref = 'refs/heads/master'
    cl.lookedup_issue = True
    self.assertIsNone(cl.EnsureAuthenticated(force=False))

  def _cmd_set_commit_gerrit_common(self, vote, notify=None):
    self.mockGit.config['branch.master.gerritissue'] = '123'
    self.mockGit.config['branch.master.gerritserver'] = (
        'https://chromium-review.googlesource.com')
    self.mockGit.config['remote.origin.url'] = (
        'https://chromium.googlesource.com/infra/infra')
    self.calls = [
        (('SetReview', 'chromium-review.googlesource.com',
          'infra%2Finfra~123', None,
          {'Commit-Queue': vote}, notify, None), ''),
    ]

  def test_cmd_set_commit_gerrit_clear(self):
    self._cmd_set_commit_gerrit_common(0)
    self.assertEqual(0, git_cl.main(['set-commit', '-c']))

  def test_cmd_set_commit_gerrit_dry(self):
    self._cmd_set_commit_gerrit_common(1, notify=False)
    self.assertEqual(0, git_cl.main(['set-commit', '-d']))

  def test_cmd_set_commit_gerrit(self):
    self._cmd_set_commit_gerrit_common(2)
    self.assertEqual(0, git_cl.main(['set-commit']))

  def test_description_display(self):
    mock.patch('git_cl.Changelist', ChangelistMock).start()
    ChangelistMock.desc = 'foo\n'

    self.assertEqual(0, git_cl.main(['description', '-d']))
    self.assertEqual('foo\n', sys.stdout.getvalue())

  @mock.patch('sys.stderr', StringIO())
  def test_StatusFieldOverrideIssueMissingArgs(self):
    try:
      self.assertEqual(git_cl.main(['status', '--issue', '1']), 0)
    except SystemExitMock:
      self.assertIn(
          '--field must be given when --issue is set.', sys.stderr.getvalue())

  def test_StatusFieldOverrideIssue(self):
    def assertIssue(cl_self, *_args):
      self.assertEqual(cl_self.issue, 1)
      return 'foobar'

    mock.patch('git_cl.Changelist.FetchDescription', assertIssue).start()
    self.assertEqual(
      git_cl.main(['status', '--issue', '1', '--field', 'desc']),
      0)
    self.assertEqual(sys.stdout.getvalue(), 'foobar\n')

  def test_SetCloseOverrideIssue(self):

    def assertIssue(cl_self, *_args):
      self.assertEqual(cl_self.issue, 1)
      return 'foobar'

    mock.patch('git_cl.Changelist.FetchDescription', assertIssue).start()
    mock.patch('git_cl.Changelist.CloseIssue', lambda *_: None).start()
    self.assertEqual(
      git_cl.main(['set-close', '--issue', '1']), 0)

  def test_description(self):
    self.mockGit.config['remote.origin.url'] = (
        'https://chromium.googlesource.com/my/repo')
    gerrit_util.GetChangeDetail.return_value = {
      'current_revision': 'sha1',
      'revisions': {'sha1': {
        'commit': {'message': 'foobar'},
      }},
    }
    self.assertEqual(0, git_cl.main([
        'description',
        'https://chromium-review.googlesource.com/c/my/repo/+/123123',
        '-d']))
    self.assertEqual('foobar\n', sys.stdout.getvalue())

  def test_description_set_raw(self):
    mock.patch('git_cl.Changelist', ChangelistMock).start()
    mock.patch('git_cl.sys.stdin', StringIO('hihi')).start()

    self.assertEqual(0, git_cl.main(['description', '-n', 'hihi']))
    self.assertEqual('hihi', ChangelistMock.desc)

  def test_description_appends_bug_line(self):
    current_desc = 'Some.\n\nChange-Id: xxx'

    def RunEditor(desc, _, **kwargs):
      self.assertEqual(
          '# Enter a description of the change.\n'
          '# This will be displayed on the codereview site.\n'
          '# The first line will also be used as the subject of the review.\n'
          '#--------------------This line is 72 characters long'
          '--------------------\n'
          'Some.\n\nChange-Id: xxx\nBug: ',
          desc)
      # Simulate user changing something.
      return 'Some.\n\nChange-Id: xxx\nBug: 123'

    def UpdateDescription(_, desc, force=False):
      self.assertEqual(desc, 'Some.\n\nChange-Id: xxx\nBug: 123')

    mock.patch('git_cl.Changelist.FetchDescription',
              lambda *args: current_desc).start()
    mock.patch('git_cl.Changelist.UpdateDescription',
              UpdateDescription).start()
    mock.patch('git_cl.gclient_utils.RunEditor', RunEditor).start()

    self.mockGit.config['branch.master.gerritissue'] = '123'
    self.assertEqual(0, git_cl.main(['description']))

  def test_description_does_not_append_bug_line_if_fixed_is_present(self):
    current_desc = 'Some.\n\nFixed: 123\nChange-Id: xxx'

    def RunEditor(desc, _, **kwargs):
      self.assertEqual(
          '# Enter a description of the change.\n'
          '# This will be displayed on the codereview site.\n'
          '# The first line will also be used as the subject of the review.\n'
          '#--------------------This line is 72 characters long'
          '--------------------\n'
          'Some.\n\nFixed: 123\nChange-Id: xxx',
          desc)
      return desc

    mock.patch('git_cl.Changelist.FetchDescription',
              lambda *args: current_desc).start()
    mock.patch('git_cl.gclient_utils.RunEditor', RunEditor).start()

    self.mockGit.config['branch.master.gerritissue'] = '123'
    self.assertEqual(0, git_cl.main(['description']))

  def test_description_set_stdin(self):
    mock.patch('git_cl.Changelist', ChangelistMock).start()
    mock.patch('git_cl.sys.stdin', StringIO('hi \r\n\t there\n\nman')).start()

    self.assertEqual(0, git_cl.main(['description', '-n', '-']))
    self.assertEqual('hi\n\t there\n\nman', ChangelistMock.desc)

  def test_archive(self):
    self.calls = [
      ((['git', 'for-each-ref', '--format=%(refname)', 'refs/heads'],),
       'refs/heads/master\nrefs/heads/foo\nrefs/heads/bar'),
      ((['git', 'for-each-ref', '--format=%(refname)', 'refs/tags'],), ''),
      ((['git', 'tag', 'git-cl-archived-456-foo', 'foo'],), ''),
      ((['git', 'branch', '-D', 'foo'],), '')
    ]

    mock.patch('git_cl.get_cl_statuses',
              lambda branches, fine_grained, max_processes:
              [(MockChangelistWithBranchAndIssue('master', 1), 'open'),
               (MockChangelistWithBranchAndIssue('foo', 456), 'closed'),
               (MockChangelistWithBranchAndIssue('bar', 789), 'open')]).start()

    self.assertEqual(0, git_cl.main(['archive', '-f']))

  def test_archive_tag_collision(self):
    self.calls = [
      ((['git', 'for-each-ref', '--format=%(refname)', 'refs/heads'],),
       'refs/heads/master\nrefs/heads/foo\nrefs/heads/bar'),
      ((['git', 'for-each-ref', '--format=%(refname)', 'refs/tags'],),
       'refs/tags/git-cl-archived-456-foo'),
      ((['git', 'tag', 'git-cl-archived-456-foo-2', 'foo'],), ''),
      ((['git', 'branch', '-D', 'foo'],), '')
    ]

    mock.patch('git_cl.get_cl_statuses',
              lambda branches, fine_grained, max_processes:
              [(MockChangelistWithBranchAndIssue('master', 1), 'open'),
               (MockChangelistWithBranchAndIssue('foo', 456), 'closed'),
               (MockChangelistWithBranchAndIssue('bar', 789), 'open')]).start()

    self.assertEqual(0, git_cl.main(['archive', '-f']))

  def test_archive_current_branch_fails(self):
    self.calls = [
      ((['git', 'for-each-ref', '--format=%(refname)', 'refs/heads'],),
         'refs/heads/master'),
      ((['git', 'for-each-ref', '--format=%(refname)', 'refs/tags'],), ''),
    ]

    mock.patch('git_cl.get_cl_statuses',
              lambda branches, fine_grained, max_processes:
              [(MockChangelistWithBranchAndIssue('master', 1),
              'closed')]).start()

    self.assertEqual(1, git_cl.main(['archive', '-f']))

  def test_archive_dry_run(self):
    self.calls = [
      ((['git', 'for-each-ref', '--format=%(refname)', 'refs/heads'],),
         'refs/heads/master\nrefs/heads/foo\nrefs/heads/bar'),
      ((['git', 'for-each-ref', '--format=%(refname)', 'refs/tags'],), ''),
    ]

    mock.patch('git_cl.get_cl_statuses',
              lambda branches, fine_grained, max_processes:
              [(MockChangelistWithBranchAndIssue('master', 1), 'open'),
               (MockChangelistWithBranchAndIssue('foo', 456), 'closed'),
               (MockChangelistWithBranchAndIssue('bar', 789), 'open')]).start()

    self.assertEqual(0, git_cl.main(['archive', '-f', '--dry-run']))

  def test_archive_no_tags(self):
    self.calls = [
      ((['git', 'for-each-ref', '--format=%(refname)', 'refs/heads'],),
         'refs/heads/master\nrefs/heads/foo\nrefs/heads/bar'),
      ((['git', 'for-each-ref', '--format=%(refname)', 'refs/tags'],), ''),
      ((['git', 'branch', '-D', 'foo'],), '')
    ]

    mock.patch('git_cl.get_cl_statuses',
              lambda branches, fine_grained, max_processes:
              [(MockChangelistWithBranchAndIssue('master', 1), 'open'),
               (MockChangelistWithBranchAndIssue('foo', 456), 'closed'),
               (MockChangelistWithBranchAndIssue('bar', 789), 'open')]).start()

    self.assertEqual(0, git_cl.main(['archive', '-f', '--notags']))

  def test_archive_tag_cleanup_on_branch_deletion_error(self):
    self.calls = [
      ((['git', 'for-each-ref', '--format=%(refname)', 'refs/heads'],),
         'refs/heads/master\nrefs/heads/foo\nrefs/heads/bar'),
      ((['git', 'for-each-ref', '--format=%(refname)', 'refs/tags'],), ''),
      ((['git', 'tag', 'git-cl-archived-456-foo', 'foo'],),
        'refs/tags/git-cl-archived-456-foo'),
      ((['git', 'branch', '-D', 'foo'],), CERR1),
      ((['git', 'tag', '-d', 'git-cl-archived-456-foo'],),
       'refs/tags/git-cl-archived-456-foo'),
    ]

    mock.patch('git_cl.get_cl_statuses',
              lambda branches, fine_grained, max_processes:
              [(MockChangelistWithBranchAndIssue('master', 1), 'open'),
               (MockChangelistWithBranchAndIssue('foo', 456), 'closed'),
               (MockChangelistWithBranchAndIssue('bar', 789), 'open')]).start()

    self.assertEqual(0, git_cl.main(['archive', '-f']))

  def test_archive_with_format(self):
    self.calls = [
        ((['git', 'for-each-ref', '--format=%(refname)', 'refs/heads'], ),
         'refs/heads/master\nrefs/heads/foo\nrefs/heads/bar'),
        ((['git', 'for-each-ref', '--format=%(refname)', 'refs/tags'], ), ''),
        ((['git', 'tag', 'archived/12-foo', 'foo'], ), ''),
        ((['git', 'branch', '-D', 'foo'], ), ''),
    ]

    mock.patch('git_cl.get_cl_statuses',
              lambda branches, fine_grained, max_processes:
              [(MockChangelistWithBranchAndIssue('foo', 12), 'closed')]).start()

    self.assertEqual(
        0, git_cl.main(['archive', '-f', '-p', 'archived/{issue}-{branch}']))

  def test_cmd_issue_erase_existing(self):
    self.mockGit.config['branch.master.gerritissue'] = '123'
    self.mockGit.config['branch.master.gerritserver'] = (
         'https://chromium-review.googlesource.com')
    self.calls = [
        ((['git', 'log', '-1', '--format=%B'],), 'This is a description'),
    ]
    self.assertEqual(0, git_cl.main(['issue', '0']))
    self.assertNotIn('branch.master.gerritissue', self.mockGit.config)
    self.assertNotIn('branch.master.gerritserver', self.mockGit.config)

  def test_cmd_issue_erase_existing_with_change_id(self):
    self.mockGit.config['branch.master.gerritissue'] = '123'
    self.mockGit.config['branch.master.gerritserver'] = (
         'https://chromium-review.googlesource.com')
    mock.patch('git_cl.Changelist.FetchDescription',
              lambda _: 'This is a description\n\nChange-Id: Ideadbeef').start()
    self.calls = [
        ((['git', 'log', '-1', '--format=%B'],),
         'This is a description\n\nChange-Id: Ideadbeef'),
        ((['git', 'commit', '--amend', '-m', 'This is a description\n'],), ''),
    ]
    self.assertEqual(0, git_cl.main(['issue', '0']))
    self.assertNotIn('branch.master.gerritissue', self.mockGit.config)
    self.assertNotIn('branch.master.gerritserver', self.mockGit.config)

  def test_cmd_issue_json(self):
    self.mockGit.config['branch.master.gerritissue'] = '123'
    self.mockGit.config['branch.master.gerritserver'] = (
         'https://chromium-review.googlesource.com')
    self.calls = [
        (('write_json', 'output.json',
          {'issue': 123,
           'issue_url': 'https://chromium-review.googlesource.com/123'}),
         ''),
    ]
    self.assertEqual(0, git_cl.main(['issue', '--json', 'output.json']))

  def _common_GerritCommitMsgHookCheck(self):
    mock.patch(
        'git_cl.os.path.abspath',
        lambda path: self._mocked_call(['abspath', path])).start()
    mock.patch(
        'git_cl.os.path.exists',
        lambda path: self._mocked_call(['exists', path])).start()
    mock.patch(
        'git_cl.gclient_utils.FileRead',
        lambda path: self._mocked_call(['FileRead', path])).start()
    mock.patch(
        'git_cl.gclient_utils.rm_file_or_tree',
        lambda path: self._mocked_call(['rm_file_or_tree', path])).start()
    mock.patch(
        'gclient_utils.AskForData',
        lambda prompt: self._mocked_call('ask_for_data', prompt)).start()
    return git_cl.Changelist(issue=123)

  def test_GerritCommitMsgHookCheck_custom_hook(self):
    cl = self._common_GerritCommitMsgHookCheck()
    self.calls += [((['exists',
                      os.path.join('.git', 'hooks', 'commit-msg')], ), True),
                   ((['FileRead',
                      os.path.join('.git', 'hooks', 'commit-msg')], ),
                    '#!/bin/sh\necho "custom hook"')]
    cl._GerritCommitMsgHookCheck(offer_removal=True)

  def test_GerritCommitMsgHookCheck_not_exists(self):
    cl = self._common_GerritCommitMsgHookCheck()
    self.calls += [
        ((['exists', os.path.join('.git', 'hooks', 'commit-msg')], ), False),
    ]
    cl._GerritCommitMsgHookCheck(offer_removal=True)

  def test_GerritCommitMsgHookCheck(self):
    cl = self._common_GerritCommitMsgHookCheck()
    self.calls += [
        ((['exists', os.path.join('.git', 'hooks', 'commit-msg')], ), True),
        ((['FileRead', os.path.join('.git', 'hooks', 'commit-msg')], ),
         '...\n# From Gerrit Code Review\n...\nadd_ChangeId()\n'),
        (('ask_for_data', 'Do you want to remove it now? [Yes/No]: '), 'Yes'),
        ((['rm_file_or_tree',
           os.path.join('.git', 'hooks', 'commit-msg')], ), ''),
    ]
    cl._GerritCommitMsgHookCheck(offer_removal=True)

  def test_GerritCmdLand(self):
    self.mockGit.config['branch.master.gerritsquashhash'] = 'deadbeaf'
    self.mockGit.config['branch.master.gerritserver'] = (
        'chromium-review.googlesource.com')
    self.calls += [
      ((['git', 'diff', 'deadbeaf'],), ''),  # No diff.
    ]
    cl = git_cl.Changelist(issue=123)
    cl._GetChangeDetail = lambda *args, **kwargs: {
      'labels': {},
      'current_revision': 'deadbeaf',
    }
    cl._GetChangeCommit = lambda: {
      'commit': 'deadbeef',
      'web_links': [{'name': 'gitiles',
                     'url': 'https://git.googlesource.com/test/+/deadbeef'}],
    }
    cl.SubmitIssue = lambda wait_for_merge: None
    self.assertEqual(0, cl.CMDLand(force=True,
                                   bypass_hooks=True,
                                   verbose=True,
                                   parallel=False))
    self.assertIn(
        'Issue chromium-review.googlesource.com/123 has been submitted',
        sys.stdout.getvalue())
    self.assertIn(
        'Landed as: https://git.googlesource.com/test/+/deadbeef',
        sys.stdout.getvalue())

  def _mock_gerrit_changes_for_detail_cache(self):
    mock.patch('git_cl.Changelist._GetGerritHost', lambda _: 'host').start()

  def test_gerrit_change_detail_cache_simple(self):
    self._mock_gerrit_changes_for_detail_cache()
    gerrit_util.GetChangeDetail.side_effect = ['a', 'b']
    cl1 = git_cl.Changelist(issue=1)
    cl1._cached_remote_url = (
        True, 'https://chromium.googlesource.com/a/my/repo.git/')
    cl2 = git_cl.Changelist(issue=2)
    cl2._cached_remote_url = (
        True, 'https://chromium.googlesource.com/ab/repo')
    self.assertEqual(cl1._GetChangeDetail(), 'a')  # Miss.
    self.assertEqual(cl1._GetChangeDetail(), 'a')
    self.assertEqual(cl2._GetChangeDetail(), 'b')  # Miss.

  def test_gerrit_change_detail_cache_options(self):
    self._mock_gerrit_changes_for_detail_cache()
    gerrit_util.GetChangeDetail.side_effect = ['cab', 'ad']
    cl = git_cl.Changelist(issue=1)
    cl._cached_remote_url = (True, 'https://chromium.googlesource.com/repo/')
    self.assertEqual(cl._GetChangeDetail(options=['C', 'A', 'B']), 'cab')
    self.assertEqual(cl._GetChangeDetail(options=['A', 'B', 'C']), 'cab')
    self.assertEqual(cl._GetChangeDetail(options=['B', 'A']), 'cab')
    self.assertEqual(cl._GetChangeDetail(options=['C']), 'cab')
    self.assertEqual(cl._GetChangeDetail(options=['A']), 'cab')
    self.assertEqual(cl._GetChangeDetail(), 'cab')

    self.assertEqual(cl._GetChangeDetail(options=['A', 'D']), 'ad')
    self.assertEqual(cl._GetChangeDetail(options=['A']), 'cab')
    self.assertEqual(cl._GetChangeDetail(options=['D']), 'ad')
    self.assertEqual(cl._GetChangeDetail(), 'cab')

  def test_gerrit_description_caching(self):
    gerrit_util.GetChangeDetail.return_value = {
      'current_revision': 'rev1',
      'revisions': {
        'rev1': {'commit': {'message': 'desc1'}},
      },
    }

    self._mock_gerrit_changes_for_detail_cache()
    cl = git_cl.Changelist(issue=1)
    cl._cached_remote_url = (
        True, 'https://chromium.googlesource.com/a/my/repo.git/')
    self.assertEqual(cl.FetchDescription(), 'desc1')
    self.assertEqual(cl.FetchDescription(), 'desc1')  # cache hit.

  def test_print_current_creds(self):
    class CookiesAuthenticatorMock(object):
      def __init__(self):
        self.gitcookies = {
            'host.googlesource.com': ('user', 'pass'),
            'host-review.googlesource.com': ('user', 'pass'),
        }
        self.netrc = self
        self.netrc.hosts = {
            'github.com': ('user2', None, 'pass2'),
            'host2.googlesource.com': ('user3', None, 'pass'),
        }
    mock.patch('git_cl.gerrit_util.CookiesAuthenticator',
              CookiesAuthenticatorMock).start()
    git_cl._GitCookiesChecker().print_current_creds(include_netrc=True)
    self.assertEqual(list(sys.stdout.getvalue().splitlines()), [
        '                        Host\t User\t Which file',
        '============================\t=====\t===========',
        'host-review.googlesource.com\t user\t.gitcookies',
        '       host.googlesource.com\t user\t.gitcookies',
        '      host2.googlesource.com\tuser3\t     .netrc',
    ])
    sys.stdout.seek(0)
    sys.stdout.truncate(0)
    git_cl._GitCookiesChecker().print_current_creds(include_netrc=False)
    self.assertEqual(list(sys.stdout.getvalue().splitlines()), [
        '                        Host\tUser\t Which file',
        '============================\t====\t===========',
        'host-review.googlesource.com\tuser\t.gitcookies',
        '       host.googlesource.com\tuser\t.gitcookies',
    ])

  def _common_creds_check_mocks(self):
    def exists_mock(path):
      dirname = os.path.dirname(path)
      if dirname == os.path.expanduser('~'):
        dirname = '~'
      base = os.path.basename(path)
      if base in (NETRC_FILENAME, '.gitcookies'):
        return self._mocked_call('os.path.exists', os.path.join(dirname, base))
      # git cl also checks for existence other files not relevant to this test.
      return None
    mock.patch(
        'gclient_utils.AskForData',
        lambda prompt: self._mocked_call('ask_for_data', prompt)).start()
    mock.patch('os.path.exists', exists_mock).start()

  def test_creds_check_gitcookies_not_configured(self):
    self._common_creds_check_mocks()
    mock.patch('git_cl._GitCookiesChecker.get_hosts_with_creds',
              lambda _, include_netrc=False: []).start()
    self.calls = [
        ((['git', 'config', '--path', 'http.cookiefile'], ), CERR1),
        ((['git', 'config', '--global', 'http.cookiefile'], ), CERR1),
        (('os.path.exists', os.path.join('~', NETRC_FILENAME)), True),
        (('ask_for_data', 'Press Enter to setup .gitcookies, '
          'or Ctrl+C to abort'), ''),
        (([
            'git', 'config', '--global', 'http.cookiefile',
            os.path.expanduser(os.path.join('~', '.gitcookies'))
        ], ), ''),
    ]
    self.assertEqual(0, git_cl.main(['creds-check']))
    self.assertTrue(
        sys.stdout.getvalue().startswith(
            'You seem to be using outdated .netrc for git credentials:'))
    self.assertIn(
        '\nConfigured git to use .gitcookies from',
        sys.stdout.getvalue())

  def test_creds_check_gitcookies_configured_custom_broken(self):
    self._common_creds_check_mocks()
    mock.patch('git_cl._GitCookiesChecker.get_hosts_with_creds',
              lambda _, include_netrc=False: []).start()
    custom_cookie_path = ('C:\\.gitcookies'
                          if sys.platform == 'win32' else '/custom/.gitcookies')
    self.calls = [
        ((['git', 'config', '--path', 'http.cookiefile'], ), CERR1),
        ((['git', 'config', '--global', 'http.cookiefile'], ),
         custom_cookie_path),
        (('os.path.exists', custom_cookie_path), False),
        (('ask_for_data', 'Reconfigure git to use default .gitcookies? '
          'Press Enter to reconfigure, or Ctrl+C to abort'), ''),
        (([
            'git', 'config', '--global', 'http.cookiefile',
            os.path.expanduser(os.path.join('~', '.gitcookies'))
        ], ), ''),
    ]
    self.assertEqual(0, git_cl.main(['creds-check']))
    self.assertIn(
        'WARNING: You have configured custom path to .gitcookies: ',
        sys.stdout.getvalue())
    self.assertIn(
        'However, your configured .gitcookies file is missing.',
        sys.stdout.getvalue())

  def test_git_cl_comment_add_gerrit(self):
    self.mockGit.branchref = None
    self.mockGit.config['remote.origin.url'] = (
        'https://chromium.googlesource.com/infra/infra')
    self.calls = [
      (('SetReview', 'chromium-review.googlesource.com', 'infra%2Finfra~10',
        'msg', None, None, None),
       None),
    ]
    self.assertEqual(0, git_cl.main(['comment', '-i', '10', '-a', 'msg']))

  @mock.patch('git_cl.Changelist.GetBranch', return_value='foo')
  def test_git_cl_comments_fetch_gerrit(self, *_mocks):
    self.mockGit.config['remote.origin.url'] = (
        'https://chromium.googlesource.com/infra/infra')
    gerrit_util.GetChangeDetail.return_value = {
        'owner': {'email': 'owner@example.com'},
        'current_revision': 'ba5eba11',
        'revisions': {
          'deadbeaf': {
            '_number': 1,
          },
          'ba5eba11': {
            '_number': 2,
          },
        },
        'messages': [
          {
             u'_revision_number': 1,
             u'author': {
               u'_account_id': 1111084,
               u'email': u'could-be-anything@example.com',
               u'name': u'LUCI CQ'
             },
             u'date': u'2017-03-15 20:08:45.000000000',
             u'id': u'f5a6c25ecbd3b3b54a43ae418ed97eff046dc50b',
             u'message': u'Patch Set 1:\n\nDry run: CQ is trying the patch...',
             u'tag': u'autogenerated:cq:dry-run'
          },
          {
             u'_revision_number': 2,
             u'author': {
               u'_account_id': 11151243,
               u'email': u'owner@example.com',
               u'name': u'owner'
             },
             u'date': u'2017-03-16 20:00:41.000000000',
             u'id': u'f5a6c25ecbd3b3b54a43ae418ed97eff046d1234',
             u'message': u'PTAL',
          },
          {
             u'_revision_number': 2,
             u'author': {
               u'_account_id': 148512,
               u'email': u'reviewer@example.com',
               u'name': u'reviewer'
             },
             u'date': u'2017-03-17 05:19:37.500000000',
             u'id': u'f5a6c25ecbd3b3b54a43ae418ed97eff046d4568',
             u'message': u'Patch Set 2: Code-Review+1',
          },
        ]
      }
    self.calls = [
      (('GetChangeComments', 'chromium-review.googlesource.com',
        'infra%2Finfra~1'), {
        '/COMMIT_MSG': [
          {
            'author': {'email': u'reviewer@example.com'},
            'updated': u'2017-03-17 05:19:37.500000000',
            'patch_set': 2,
            'side': 'REVISION',
            'message': 'Please include a bug link',
          },
        ],
        'codereview.settings': [
          {
            'author': {'email': u'owner@example.com'},
            'updated': u'2017-03-16 20:00:41.000000000',
            'patch_set': 2,
            'side': 'PARENT',
            'line': 42,
            'message': 'I removed this because it is bad',
          },
        ]
      }),
      (('GetChangeRobotComments', 'chromium-review.googlesource.com',
        'infra%2Finfra~1'), {}),
    ] * 2 + [
      (('write_json', 'output.json', [
        {
          u'date': u'2017-03-16 20:00:41.000000',
          u'message': (
              u'PTAL\n' +
              u'\n' +
              u'codereview.settings\n' +
              u'  Base, Line 42: https://chromium-review.googlesource.com/' +
              u'c/1/2/codereview.settings#b42\n' +
              u'  I removed this because it is bad\n'),
          u'autogenerated': False,
          u'approval': False,
          u'disapproval': False,
          u'sender': u'owner@example.com'
        }, {
          u'date': u'2017-03-17 05:19:37.500000',
          u'message': (
              u'Patch Set 2: Code-Review+1\n' +
              u'\n' +
              u'/COMMIT_MSG\n' +
              u'  PS2, File comment: https://chromium-review.googlesource' +
              u'.com/c/1/2//COMMIT_MSG#\n' +
              u'  Please include a bug link\n'),
          u'autogenerated': False,
          u'approval': False,
          u'disapproval': False,
          u'sender': u'reviewer@example.com'
        }
      ]), '')
    ]
    expected_comments_summary = [
      git_cl._CommentSummary(
        message=(
            u'PTAL\n' +
            u'\n' +
            u'codereview.settings\n' +
            u'  Base, Line 42: https://chromium-review.googlesource.com/' +
            u'c/1/2/codereview.settings#b42\n' +
            u'  I removed this because it is bad\n'),
        date=datetime.datetime(2017, 3, 16, 20, 0, 41, 0),
        autogenerated=False,
        disapproval=False, approval=False, sender=u'owner@example.com'),
      git_cl._CommentSummary(
        message=(
            u'Patch Set 2: Code-Review+1\n' +
            u'\n' +
            u'/COMMIT_MSG\n' +
            u'  PS2, File comment: https://chromium-review.googlesource.com/' +
            u'c/1/2//COMMIT_MSG#\n' +
            u'  Please include a bug link\n'),
        date=datetime.datetime(2017, 3, 17, 5, 19, 37, 500000),
        autogenerated=False,
        disapproval=False, approval=False, sender=u'reviewer@example.com'),
    ]
    cl = git_cl.Changelist(
        issue=1, branchref='refs/heads/foo')
    self.assertEqual(cl.GetCommentsSummary(), expected_comments_summary)
    self.assertEqual(
        0, git_cl.main(['comments', '-i', '1', '-j', 'output.json']))

  def test_git_cl_comments_robot_comments(self):
    # git cl comments also fetches robot comments (which are considered a type
    # of autogenerated comment), and unlike other types of comments, only robot
    # comments from the latest patchset are shown.
    self.mockGit.config['remote.origin.url'] = (
        'https://chromium.googlesource.com/infra/infra')
    gerrit_util.GetChangeDetail.return_value = {
      'owner': {'email': 'owner@example.com'},
      'current_revision': 'ba5eba11',
      'revisions': {
        'deadbeaf': {
          '_number': 1,
        },
        'ba5eba11': {
          '_number': 2,
        },
      },
      'messages': [
      {
        u'_revision_number': 1,
        u'author': {
          u'_account_id': 1111084,
          u'email': u'commit-bot@chromium.org',
          u'name': u'Commit Bot'
        },
        u'date': u'2017-03-15 20:08:45.000000000',
        u'id': u'f5a6c25ecbd3b3b54a43ae418ed97eff046dc50b',
        u'message': u'Patch Set 1:\n\nDry run: CQ is trying the patch...',
        u'tag': u'autogenerated:cq:dry-run'
      },
      {
        u'_revision_number': 1,
        u'author': {
          u'_account_id': 123,
          u'email': u'tricium@serviceaccount.com',
          u'name': u'Tricium'
        },
        u'date': u'2017-03-16 20:00:41.000000000',
        u'id': u'f5a6c25ecbd3b3b54a43ae418ed97eff046d1234',
        u'message': u'(1 comment)',
        u'tag': u'autogenerated:tricium',
      },
      {
        u'_revision_number': 1,
        u'author': {
          u'_account_id': 123,
          u'email': u'tricium@serviceaccount.com',
          u'name': u'Tricium'
        },
        u'date': u'2017-03-16 20:00:41.000000000',
        u'id': u'f5a6c25ecbd3b3b54a43ae418ed97eff046d1234',
        u'message': u'(1 comment)',
        u'tag': u'autogenerated:tricium',
      },
      {
        u'_revision_number': 2,
        u'author': {
          u'_account_id': 123,
          u'email': u'tricium@serviceaccount.com',
          u'name': u'reviewer'
        },
        u'date': u'2017-03-17 05:30:37.000000000',
        u'tag': u'autogenerated:tricium',
        u'id': u'f5a6c25ecbd3b3b54a43ae418ed97eff046d4568',
        u'message': u'(1 comment)',
      },
      ]
    }
    self.calls = [
      (('GetChangeComments', 'chromium-review.googlesource.com',
        'infra%2Finfra~1'), {}),
      (('GetChangeRobotComments', 'chromium-review.googlesource.com',
        'infra%2Finfra~1'), {
        'codereview.settings': [
          {
            u'author': {u'email': u'tricium@serviceaccount.com'},
            u'updated': u'2017-03-17 05:30:37.000000000',
            u'robot_run_id': u'5565031076855808',
            u'robot_id': u'Linter/Category',
            u'tag': u'autogenerated:tricium',
            u'patch_set': 2,
            u'side': u'REVISION',
            u'message': u'Linter warning message text',
            u'line': 32,
          },
        ],
      }),
    ]
    expected_comments_summary = [
       git_cl._CommentSummary(date=datetime.datetime(2017, 3, 17, 5, 30, 37),
         message=(
           u'(1 comment)\n\ncodereview.settings\n'
           u'  PS2, Line 32: https://chromium-review.googlesource.com/'
           u'c/1/2/codereview.settings#32\n'
           u'  Linter warning message text\n'),
         sender=u'tricium@serviceaccount.com',
         autogenerated=True, approval=False, disapproval=False)
    ]
    cl = git_cl.Changelist(
        issue=1, branchref='refs/heads/foo')
    self.assertEqual(cl.GetCommentsSummary(), expected_comments_summary)

  def test_get_remote_url_with_mirror(self):
    original_os_path_isdir = os.path.isdir

    def selective_os_path_isdir_mock(path):
      if path == '/cache/this-dir-exists':
        return self._mocked_call('os.path.isdir', path)
      return original_os_path_isdir(path)

    mock.patch('os.path.isdir', selective_os_path_isdir_mock).start()

    url = 'https://chromium.googlesource.com/my/repo'
    self.mockGit.config['remote.origin.url'] = (
        '/cache/this-dir-exists')
    self.mockGit.config['/cache/this-dir-exists:remote.origin.url'] = (
        url)
    self.calls = [
      (('os.path.isdir', '/cache/this-dir-exists'),
       True),
    ]
    cl = git_cl.Changelist(issue=1)
    self.assertEqual(cl.GetRemoteUrl(), url)
    self.assertEqual(cl.GetRemoteUrl(), url)  # Must be cached.

  def test_get_remote_url_non_existing_mirror(self):
    original_os_path_isdir = os.path.isdir

    def selective_os_path_isdir_mock(path):
      if path == '/cache/this-dir-doesnt-exist':
        return self._mocked_call('os.path.isdir', path)
      return original_os_path_isdir(path)

    mock.patch('os.path.isdir', selective_os_path_isdir_mock).start()
    mock.patch('logging.error',
              lambda *a: self._mocked_call('logging.error', *a)).start()

    self.mockGit.config['remote.origin.url'] = (
        '/cache/this-dir-doesnt-exist')
    self.calls = [
      (('os.path.isdir', '/cache/this-dir-doesnt-exist'),
       False),
      (('logging.error',
          'Remote "%(remote)s" for branch "%(branch)s" points to "%(url)s", '
          'but it doesn\'t exist.', {
            'remote': 'origin',
            'branch': 'master',
            'url': '/cache/this-dir-doesnt-exist'}
        ), None),
    ]
    cl = git_cl.Changelist(issue=1)
    self.assertIsNone(cl.GetRemoteUrl())

  def test_get_remote_url_misconfigured_mirror(self):
    original_os_path_isdir = os.path.isdir

    def selective_os_path_isdir_mock(path):
      if path == '/cache/this-dir-exists':
        return self._mocked_call('os.path.isdir', path)
      return original_os_path_isdir(path)

    mock.patch('os.path.isdir', selective_os_path_isdir_mock).start()
    mock.patch('logging.error',
              lambda *a: self._mocked_call('logging.error', *a)).start()

    self.mockGit.config['remote.origin.url'] = (
        '/cache/this-dir-exists')
    self.calls = [
      (('os.path.isdir', '/cache/this-dir-exists'), True),
      (('logging.error',
        'Remote "%(remote)s" for branch "%(branch)s" points to '
        '"%(cache_path)s", but it is misconfigured.\n'
        '"%(cache_path)s" must be a git repo and must have a remote named '
        '"%(remote)s" pointing to the git host.', {
              'remote': 'origin',
              'cache_path': '/cache/this-dir-exists',
              'branch': 'master'}
        ), None),
    ]
    cl = git_cl.Changelist(issue=1)
    self.assertIsNone(cl.GetRemoteUrl())

  def test_gerrit_change_identifier_with_project(self):
    self.mockGit.config['remote.origin.url'] = (
        'https://chromium.googlesource.com/a/my/repo.git/')
    cl = git_cl.Changelist(issue=123456)
    self.assertEqual(cl._GerritChangeIdentifier(), 'my%2Frepo~123456')

  def test_gerrit_change_identifier_without_project(self):
    mock.patch('logging.error',
              lambda *a: self._mocked_call('logging.error', *a)).start()

    self.calls = [
      (('logging.error',
          'Remote "%(remote)s" for branch "%(branch)s" points to "%(url)s", '
          'but it doesn\'t exist.', {
            'remote': 'origin',
            'branch': 'master',
            'url': ''}
        ), None),
    ]
    cl = git_cl.Changelist(issue=123456)
    self.assertEqual(cl._GerritChangeIdentifier(), '123456')


class ChangelistTest(unittest.TestCase):
  def setUp(self):
    super(ChangelistTest, self).setUp()
    mock.patch('gclient_utils.FileRead').start()
    mock.patch('gclient_utils.FileWrite').start()
    mock.patch('gclient_utils.temporary_file', TemporaryFileMock()).start()
    mock.patch(
        'git_cl.Changelist.GetCodereviewServer',
        return_value='https://chromium-review.googlesource.com').start()
    mock.patch('git_cl.Changelist.GetAuthor', return_value='author').start()
    mock.patch('git_cl.Changelist.GetIssue', return_value=123456).start()
    mock.patch('git_cl.Changelist.GetPatchset', return_value=7).start()
    mock.patch('git_cl.PRESUBMIT_SUPPORT', 'PRESUBMIT_SUPPORT').start()
    mock.patch('git_cl.Settings.GetRoot', return_value='root').start()
    mock.patch('git_cl.time_time').start()
    mock.patch('metrics.collector').start()
    mock.patch('subprocess2.Popen').start()
    self.addCleanup(mock.patch.stopall)
    self.temp_count = 0

  def testRunHook(self):
    expected_results = {
        'more_cc': ['more@example.com', 'cc@example.com'],
        'should_continue': True,
    }
    gclient_utils.FileRead.return_value = json.dumps(expected_results)
    git_cl.time_time.side_effect = [100, 200]
    mockProcess = mock.Mock()
    mockProcess.wait.return_value = 0
    subprocess2.Popen.return_value = mockProcess

    cl = git_cl.Changelist()
    results = cl.RunHook(
        committing=True,
        may_prompt=True,
        verbose=2,
        parallel=True,
        upstream='upstream',
        description='description',
        all_files=True,
        resultdb=False)

    self.assertEqual(expected_results, results)
    subprocess2.Popen.assert_called_once_with([
        'vpython', 'PRESUBMIT_SUPPORT',
        '--root', 'root',
        '--upstream', 'upstream',
        '--verbose', '--verbose',
        '--author', 'author',
        '--gerrit_url', 'https://chromium-review.googlesource.com',
        '--issue', '123456',
        '--patchset', '7',
        '--commit',
        '--may_prompt',
        '--parallel',
        '--all_files',
        '--json_output', '/tmp/fake-temp2',
        '--description_file', '/tmp/fake-temp1',
    ])
    gclient_utils.FileWrite.assert_called_once_with(
        '/tmp/fake-temp1', 'description')
    metrics.collector.add_repeated('sub_commands', {
      'command': 'presubmit',
      'execution_time': 100,
      'exit_code': 0,
    })

  def testRunHook_FewerOptions(self):
    expected_results = {
        'more_cc': ['more@example.com', 'cc@example.com'],
        'should_continue': True,
    }
    gclient_utils.FileRead.return_value = json.dumps(expected_results)
    git_cl.time_time.side_effect = [100, 200]
    mockProcess = mock.Mock()
    mockProcess.wait.return_value = 0
    subprocess2.Popen.return_value = mockProcess

    git_cl.Changelist.GetAuthor.return_value = None
    git_cl.Changelist.GetIssue.return_value = None
    git_cl.Changelist.GetPatchset.return_value = None
    git_cl.Changelist.GetCodereviewServer.return_value = None

    cl = git_cl.Changelist()
    results = cl.RunHook(
        committing=False,
        may_prompt=False,
        verbose=0,
        parallel=False,
        upstream='upstream',
        description='description',
        all_files=False,
        resultdb=False)

    self.assertEqual(expected_results, results)
    subprocess2.Popen.assert_called_once_with([
        'vpython', 'PRESUBMIT_SUPPORT',
        '--root', 'root',
        '--upstream', 'upstream',
        '--upload',
        '--json_output', '/tmp/fake-temp2',
        '--description_file', '/tmp/fake-temp1',
    ])
    gclient_utils.FileWrite.assert_called_once_with(
        '/tmp/fake-temp1', 'description')
    metrics.collector.add_repeated('sub_commands', {
      'command': 'presubmit',
      'execution_time': 100,
      'exit_code': 0,
    })

  def testRunHook_FewerOptionsResultDB(self):
    expected_results = {
      'more_cc': ['more@example.com', 'cc@example.com'],
      'should_continue': True,
    }
    gclient_utils.FileRead.return_value = json.dumps(expected_results)
    git_cl.time_time.side_effect = [100, 200]
    mockProcess = mock.Mock()
    mockProcess.wait.return_value = 0
    subprocess2.Popen.return_value = mockProcess

    git_cl.Changelist.GetAuthor.return_value = None
    git_cl.Changelist.GetIssue.return_value = None
    git_cl.Changelist.GetPatchset.return_value = None
    git_cl.Changelist.GetCodereviewServer.return_value = None

    cl = git_cl.Changelist()
    results = cl.RunHook(
        committing=False,
        may_prompt=False,
        verbose=0,
        parallel=False,
        upstream='upstream',
        description='description',
        all_files=False,
        resultdb=True)

    self.assertEqual(expected_results, results)
    subprocess2.Popen.assert_called_once_with([
        'rdb', 'stream', '-new',
        'vpython', 'PRESUBMIT_SUPPORT',
        '--root', 'root',
        '--upstream', 'upstream',
        '--upload',
        '--json_output', '/tmp/fake-temp2',
        '--description_file', '/tmp/fake-temp1',
    ])

  @mock.patch('sys.exit', side_effect=SystemExitMock)
  def testRunHook_Failure(self, _mock):
    git_cl.time_time.side_effect = [100, 200]
    mockProcess = mock.Mock()
    mockProcess.wait.return_value = 2
    subprocess2.Popen.return_value = mockProcess

    cl = git_cl.Changelist()
    with self.assertRaises(SystemExitMock):
      cl.RunHook(
          committing=True,
          may_prompt=True,
          verbose=2,
          parallel=True,
          upstream='upstream',
          description='description',
          all_files=True,
          resultdb=False)

    sys.exit.assert_called_once_with(2)

  def testRunPostUploadHook(self):
    cl = git_cl.Changelist()
    cl.RunPostUploadHook(2, 'upstream', 'description')

    subprocess2.Popen.assert_called_once_with([
        'vpython', 'PRESUBMIT_SUPPORT',
        '--root', 'root',
        '--upstream', 'upstream',
        '--verbose', '--verbose',
        '--author', 'author',
        '--gerrit_url', 'https://chromium-review.googlesource.com',
        '--issue', '123456',
        '--patchset', '7',
        '--post_upload',
        '--description_file', '/tmp/fake-temp1',
    ])
    gclient_utils.FileWrite.assert_called_once_with(
        '/tmp/fake-temp1', 'description')


class CMDTestCaseBase(unittest.TestCase):
  _STATUSES = [
      'STATUS_UNSPECIFIED', 'SCHEDULED', 'STARTED', 'SUCCESS', 'FAILURE',
      'INFRA_FAILURE', 'CANCELED',
  ]
  _CHANGE_DETAIL = {
      'project': 'depot_tools',
      'status': 'OPEN',
      'owner': {'email': 'owner@e.mail'},
      'current_revision': 'beeeeeef',
      'revisions': {
          'deadbeaf': {'_number': 6},
          'beeeeeef': {
              '_number': 7,
              'fetch': {'http': {
                  'url': 'https://chromium.googlesource.com/depot_tools',
                  'ref': 'refs/changes/56/123456/7'
              }},
          },
      },
  }
  _DEFAULT_RESPONSE = {
      'builds': [{
          'id': str(100 + idx),
          'builder': {
              'project': 'chromium',
              'bucket': 'try',
              'builder': 'bot_' + status.lower(),
          },
          'createTime': '2019-10-09T08:00:0%d.854286Z' % (idx % 10),
          'tags': [],
          'status': status,
      } for idx, status in enumerate(_STATUSES)]
  }

  def setUp(self):
    super(CMDTestCaseBase, self).setUp()
    mock.patch('git_cl.sys.stdout', StringIO()).start()
    mock.patch('git_cl.uuid.uuid4', return_value='uuid4').start()
    mock.patch('git_cl.Changelist.GetIssue', return_value=123456).start()
    mock.patch(
        'git_cl.Changelist.GetCodereviewServer',
        return_value='https://chromium-review.googlesource.com').start()
    mock.patch(
        'git_cl.Changelist._GetGerritHost',
        return_value='chromium-review.googlesource.com').start()
    mock.patch(
        'git_cl.Changelist.GetMostRecentPatchset',
        return_value=7).start()
    mock.patch(
        'git_cl.Changelist.GetRemoteUrl',
        return_value='https://chromium.googlesource.com/depot_tools').start()
    mock.patch(
        'auth.Authenticator',
        return_value=AuthenticatorMock()).start()
    mock.patch(
        'gerrit_util.GetChangeDetail',
        return_value=self._CHANGE_DETAIL).start()
    mock.patch(
        'git_cl._call_buildbucket',
        return_value = self._DEFAULT_RESPONSE).start()
    mock.patch('git_common.is_dirty_git_tree', return_value=False).start()
    self.addCleanup(mock.patch.stopall)


class CMDPresubmitTestCase(CMDTestCaseBase):
  def setUp(self):
    super(CMDPresubmitTestCase, self).setUp()
    mock.patch(
       'git_cl.Changelist.GetCommonAncestorWithUpstream',
       return_value='upstream').start()
    mock.patch(
        'git_cl.Changelist.FetchDescription',
        return_value='fetch description').start()
    mock.patch(
        'git_cl._create_description_from_log',
        return_value='get description').start()
    mock.patch('git_cl.Changelist.RunHook').start()

  def testDefaultCase(self):
    self.assertEqual(0, git_cl.main(['presubmit']))
    git_cl.Changelist.RunHook.assert_called_once_with(
        committing=True,
        may_prompt=False,
        verbose=0,
        parallel=None,
        upstream='upstream',
        description='fetch description',
        all_files=None,
        resultdb=None)

  def testNoIssue(self):
    git_cl.Changelist.GetIssue.return_value = None
    self.assertEqual(0, git_cl.main(['presubmit']))
    git_cl.Changelist.RunHook.assert_called_once_with(
        committing=True,
        may_prompt=False,
        verbose=0,
        parallel=None,
        upstream='upstream',
        description='get description',
        all_files=None,
        resultdb=None)

  def testCustomBranch(self):
    self.assertEqual(0, git_cl.main(['presubmit', 'custom_branch']))
    git_cl.Changelist.RunHook.assert_called_once_with(
        committing=True,
        may_prompt=False,
        verbose=0,
        parallel=None,
        upstream='custom_branch',
        description='fetch description',
        all_files=None,
        resultdb=None)

  def testOptions(self):
    self.assertEqual(
        0, git_cl.main(['presubmit', '-v', '-v', '--all', '--parallel', '-u',
                          '--resultdb']))
    git_cl.Changelist.RunHook.assert_called_once_with(
        committing=False,
        may_prompt=False,
        verbose=2,
        parallel=True,
        upstream='upstream',
        description='fetch description',
        all_files=True,
        resultdb=True)

class CMDTryResultsTestCase(CMDTestCaseBase):
  _DEFAULT_REQUEST = {
      'predicate': {
          "gerritChanges": [{
              "project": "depot_tools",
              "host": "chromium-review.googlesource.com",
              "patchset": 7,
              "change": 123456,
          }],
      },
      'fields': ('builds.*.id,builds.*.builder,builds.*.status' +
                 ',builds.*.createTime,builds.*.tags'),
  }

  def testNoJobs(self):
    git_cl._call_buildbucket.return_value = {}

    self.assertEqual(0, git_cl.main(['try-results']))
    self.assertEqual('No tryjobs scheduled.\n', sys.stdout.getvalue())
    git_cl._call_buildbucket.assert_called_once_with(
        mock.ANY, 'cr-buildbucket.appspot.com', 'SearchBuilds',
        self._DEFAULT_REQUEST)

  def testPrintToStdout(self):
    self.assertEqual(0, git_cl.main(['try-results']))
    self.assertEqual([
        'Successes:',
        '  bot_success            https://ci.chromium.org/b/103',
        'Infra Failures:',
        '  bot_infra_failure      https://ci.chromium.org/b/105',
        'Failures:',
        '  bot_failure            https://ci.chromium.org/b/104',
        'Canceled:',
        '  bot_canceled          ',
        'Started:',
        '  bot_started            https://ci.chromium.org/b/102',
        'Scheduled:',
        '  bot_scheduled          id=101',
        'Other:',
        '  bot_status_unspecified id=100',
        'Total: 7 tryjobs',
    ], sys.stdout.getvalue().splitlines())
    git_cl._call_buildbucket.assert_called_once_with(
        mock.ANY, 'cr-buildbucket.appspot.com', 'SearchBuilds',
        self._DEFAULT_REQUEST)

  def testPrintToStdoutWithMasters(self):
    self.assertEqual(0, git_cl.main(['try-results', '--print-master']))
    self.assertEqual([
        'Successes:',
        '  try bot_success            https://ci.chromium.org/b/103',
        'Infra Failures:',
        '  try bot_infra_failure      https://ci.chromium.org/b/105',
        'Failures:',
        '  try bot_failure            https://ci.chromium.org/b/104',
        'Canceled:',
        '  try bot_canceled          ',
        'Started:',
        '  try bot_started            https://ci.chromium.org/b/102',
        'Scheduled:',
        '  try bot_scheduled          id=101',
        'Other:',
        '  try bot_status_unspecified id=100',
        'Total: 7 tryjobs',
    ], sys.stdout.getvalue().splitlines())
    git_cl._call_buildbucket.assert_called_once_with(
        mock.ANY, 'cr-buildbucket.appspot.com', 'SearchBuilds',
        self._DEFAULT_REQUEST)

  @mock.patch('git_cl.write_json')
  def testWriteToJson(self, mockJsonDump):
    self.assertEqual(0, git_cl.main(['try-results', '--json', 'file.json']))
    git_cl._call_buildbucket.assert_called_once_with(
        mock.ANY, 'cr-buildbucket.appspot.com', 'SearchBuilds',
        self._DEFAULT_REQUEST)
    mockJsonDump.assert_called_once_with(
        'file.json', self._DEFAULT_RESPONSE['builds'])

  def test_filter_failed_for_one_simple(self):
    self.assertEqual([], git_cl._filter_failed_for_retry([]))
    self.assertEqual(
        [
            ('chromium', 'try', 'bot_failure'),
            ('chromium', 'try', 'bot_infra_failure'),
        ],
        git_cl._filter_failed_for_retry(self._DEFAULT_RESPONSE['builds']))

  def test_filter_failed_for_retry_many_builds(self):

    def _build(name, created_sec, status, experimental=False):
      assert 0 <= created_sec < 100, created_sec
      b = {
          'id': 112112,
          'builder': {
              'project': 'chromium',
              'bucket': 'try',
              'builder': name,
          },
          'createTime': '2019-10-09T08:00:%02d.854286Z' % created_sec,
          'status': status,
          'tags': [],
      }
      if experimental:
        b['tags'].append({'key': 'cq_experimental', 'value': 'true'})
      return b

    builds = [
        _build('flaky-last-green', 1, 'FAILURE'),
        _build('flaky-last-green', 2, 'SUCCESS'),
        _build('flaky', 1, 'SUCCESS'),
        _build('flaky', 2, 'FAILURE'),
        _build('running', 1, 'FAILED'),
        _build('running', 2, 'SCHEDULED'),
        _build('yep-still-running', 1, 'STARTED'),
        _build('yep-still-running', 2, 'FAILURE'),
        _build('cq-experimental', 1, 'SUCCESS', experimental=True),
        _build('cq-experimental', 2, 'FAILURE', experimental=True),

        # Simulate experimental in CQ builder, which developer decided
        # to retry manually which resulted in 2nd build non-experimental.
        _build('sometimes-experimental', 1, 'FAILURE', experimental=True),
        _build('sometimes-experimental', 2, 'FAILURE', experimental=False),
    ]
    builds.sort(key=lambda b: b['status'])  # ~deterministic shuffle.
    self.assertEqual(
        [
            ('chromium', 'try', 'flaky'),
            ('chromium', 'try', 'sometimes-experimental'),
        ],
        git_cl._filter_failed_for_retry(builds))


class CMDTryTestCase(CMDTestCaseBase):

  @mock.patch('git_cl.Changelist.SetCQState')
  def testSetCQDryRunByDefault(self, mockSetCQState):
    mockSetCQState.return_value = 0
    self.assertEqual(0, git_cl.main(['try']))
    git_cl.Changelist.SetCQState.assert_called_with(git_cl._CQState.DRY_RUN)
    self.assertEqual(
        sys.stdout.getvalue(),
        'Scheduling CQ dry run on: '
        'https://chromium-review.googlesource.com/123456\n')

  @mock.patch('git_cl._call_buildbucket')
  def testScheduleOnBuildbucket(self, mockCallBuildbucket):
    mockCallBuildbucket.return_value = {}

    self.assertEqual(0, git_cl.main([
        'try', '-B', 'luci.chromium.try', '-b', 'win',
        '-p', 'key=val', '-p', 'json=[{"a":1}, null]']))
    self.assertIn(
        'Scheduling jobs on:\n'
        '  chromium/try: win',
        git_cl.sys.stdout.getvalue())

    expected_request = {
        "requests": [{
            "scheduleBuild": {
                "requestId": "uuid4",
                "builder": {
                    "project": "chromium",
                    "builder": "win",
                    "bucket": "try",
                },
                "gerritChanges": [{
                    "project": "depot_tools",
                    "host": "chromium-review.googlesource.com",
                    "patchset": 7,
                    "change": 123456,
                }],
                "properties": {
                    "category": "git_cl_try",
                    "json": [{"a": 1}, None],
                    "key": "val",
                },
                "tags": [
                    {"value": "win", "key": "builder"},
                    {"value": "git_cl_try", "key": "user_agent"},
                ],
            },
        }],
    }
    mockCallBuildbucket.assert_called_with(
        mock.ANY, 'cr-buildbucket.appspot.com', 'Batch', expected_request)

  @mock.patch('git_cl._call_buildbucket')
  def testScheduleOnBuildbucketWithRevision(self, mockCallBuildbucket):
    mockCallBuildbucket.return_value = {}

    self.assertEqual(0, git_cl.main([
        'try', '-B', 'luci.chromium.try', '-b', 'win', '-b', 'linux',
        '-p', 'key=val', '-p', 'json=[{"a":1}, null]',
        '-r', 'beeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeef']))
    self.assertIn(
        'Scheduling jobs on:\n'
        '  chromium/try: linux\n'
        '  chromium/try: win',
        git_cl.sys.stdout.getvalue())

    expected_request = {
        "requests": [{
            "scheduleBuild": {
                "requestId": "uuid4",
                "builder": {
                    "project": "chromium",
                    "builder": "linux",
                    "bucket": "try",
                },
                "gerritChanges": [{
                    "project": "depot_tools",
                    "host": "chromium-review.googlesource.com",
                    "patchset": 7,
                    "change": 123456,
                }],
                "properties": {
                    "category": "git_cl_try",
                    "json": [{"a": 1}, None],
                    "key": "val",
                },
                "tags": [
                    {"value": "linux", "key": "builder"},
                    {"value": "git_cl_try", "key": "user_agent"},
                ],
                "gitilesCommit": {
                    "host": "chromium-review.googlesource.com",
                    "project": "depot_tools",
                    "id": "beeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeef",
                }
            },
        },
        {
            "scheduleBuild": {
                "requestId": "uuid4",
                "builder": {
                    "project": "chromium",
                    "builder": "win",
                    "bucket": "try",
                },
                "gerritChanges": [{
                    "project": "depot_tools",
                    "host": "chromium-review.googlesource.com",
                    "patchset": 7,
                    "change": 123456,
                }],
                "properties": {
                    "category": "git_cl_try",
                    "json": [{"a": 1}, None],
                    "key": "val",
                },
                "tags": [
                    {"value": "win", "key": "builder"},
                    {"value": "git_cl_try", "key": "user_agent"},
                ],
                "gitilesCommit": {
                    "host": "chromium-review.googlesource.com",
                    "project": "depot_tools",
                    "id": "beeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeef",
                }
            },
        }],
    }
    mockCallBuildbucket.assert_called_with(
        mock.ANY, 'cr-buildbucket.appspot.com', 'Batch', expected_request)

  @mock.patch('sys.stderr', StringIO())
  def testScheduleOnBuildbucket_WrongBucket(self):
    with self.assertRaises(SystemExit):
      git_cl.main([
          'try', '-B', 'not-a-bucket', '-b', 'win',
          '-p', 'key=val', '-p', 'json=[{"a":1}, null]'])
    self.assertIn(
        'Invalid bucket: not-a-bucket.',
        sys.stderr.getvalue())

  @mock.patch('git_cl._call_buildbucket')
  @mock.patch('git_cl._fetch_tryjobs')
  def testScheduleOnBuildbucketRetryFailed(
      self, mockFetchTryJobs, mockCallBuildbucket):
    git_cl._fetch_tryjobs.side_effect = lambda *_, **kw: {
        7: [],
        6: [{
            'id': 112112,
            'builder': {
                'project': 'chromium',
                'bucket': 'try',
                'builder': 'linux', },
            'createTime': '2019-10-09T08:00:01.854286Z',
            'tags': [],
            'status': 'FAILURE', }], }[kw['patchset']]
    mockCallBuildbucket.return_value = {}

    self.assertEqual(0, git_cl.main(['try', '--retry-failed']))
    self.assertIn(
        'Scheduling jobs on:\n'
        '  chromium/try: linux',
        git_cl.sys.stdout.getvalue())

    expected_request = {
        "requests": [{
            "scheduleBuild": {
                "requestId": "uuid4",
                "builder": {
                    "project": "chromium",
                    "bucket": "try",
                    "builder": "linux",
                },
                "gerritChanges": [{
                    "project": "depot_tools",
                    "host": "chromium-review.googlesource.com",
                    "patchset": 7,
                    "change": 123456,
                }],
                "properties": {
                    "category": "git_cl_try",
                },
                "tags": [
                    {"value": "linux", "key": "builder"},
                    {"value": "git_cl_try", "key": "user_agent"},
                    {"value": "1", "key": "retry_failed"},
                ],
            },
        }],
    }
    mockCallBuildbucket.assert_called_with(
        mock.ANY, 'cr-buildbucket.appspot.com', 'Batch', expected_request)

  def test_parse_bucket(self):
    test_cases = [
        {
            'bucket': 'chromium/try',
            'result': ('chromium', 'try'),
        },
        {
            'bucket': 'luci.chromium.try',
            'result': ('chromium', 'try'),
            'has_warning': True,
        },
        {
            'bucket': 'skia.primary',
            'result': ('skia', 'skia.primary'),
            'has_warning': True,
        },
        {
            'bucket': 'not-a-bucket',
            'result': (None, None),
        },
    ]

    for test_case in test_cases:
      git_cl.sys.stdout.truncate(0)
      self.assertEqual(
          test_case['result'], git_cl._parse_bucket(test_case['bucket']))
      if test_case.get('has_warning'):
        expected_warning = 'WARNING Please use %s/%s to specify the bucket' % (
            test_case['result'])
        self.assertIn(expected_warning, git_cl.sys.stdout.getvalue())


class CMDUploadTestCase(CMDTestCaseBase):

  def setUp(self):
    super(CMDUploadTestCase, self).setUp()
    mock.patch('git_cl._fetch_tryjobs').start()
    mock.patch('git_cl._trigger_tryjobs', return_value={}).start()
    mock.patch('git_cl.Changelist.CMDUpload', return_value=0).start()
    mock.patch('git_cl.Settings.GetRoot', return_value='').start()
    mock.patch(
        'git_cl.Settings.GetSquashGerritUploads',
        return_value=True).start()
    self.addCleanup(mock.patch.stopall)

  def testWarmUpChangeDetailCache(self):
    self.assertEqual(0, git_cl.main(['upload']))
    gerrit_util.GetChangeDetail.assert_called_once_with(
        'chromium-review.googlesource.com', 'depot_tools~123456',
        frozenset([
            'LABELS', 'CURRENT_REVISION', 'DETAILED_ACCOUNTS',
            'CURRENT_COMMIT']))

  def testUploadRetryFailed(self):
    # This test mocks out the actual upload part, and just asserts that after
    # upload, if --retry-failed is added, then the tool will fetch try jobs
    # from the previous patchset and trigger the right builders on the latest
    # patchset.
    git_cl._fetch_tryjobs.side_effect = [
        # Latest patchset: No builds.
        [],
        # Patchset before latest: Some builds.
        [{
            'id': str(100 + idx),
            'builder': {
                'project': 'chromium',
                'bucket': 'try',
                'builder': 'bot_' + status.lower(),
            },
            'createTime': '2019-10-09T08:00:0%d.854286Z' % (idx % 10),
            'tags': [],
            'status': status,
        } for idx, status in enumerate(self._STATUSES)],
    ]

    self.assertEqual(0, git_cl.main(['upload', '--retry-failed']))
    self.assertEqual([
        mock.call(mock.ANY, 'cr-buildbucket.appspot.com', patchset=7),
        mock.call(mock.ANY, 'cr-buildbucket.appspot.com', patchset=6),
    ], git_cl._fetch_tryjobs.mock_calls)
    expected_buckets = [
        ('chromium', 'try', 'bot_failure'),
        ('chromium', 'try', 'bot_infra_failure'),
    ]
    git_cl._trigger_tryjobs.assert_called_once_with(mock.ANY, expected_buckets,
                                                    mock.ANY, 8)


class MakeRequestsHelperTestCase(unittest.TestCase):

  def exampleGerritChange(self):
    return {
        'host': 'chromium-review.googlesource.com',
        'project': 'depot_tools',
        'change': 1,
        'patchset': 2,
    }

  def testMakeRequestsHelperNoOptions(self):
    # Basic test for the helper function _make_tryjob_schedule_requests;
    # it shouldn't throw AttributeError even when options doesn't have any
    # of the expected values; it will use default option values.
    changelist = ChangelistMock(gerrit_change=self.exampleGerritChange())
    jobs = [('chromium', 'try', 'my-builder')]
    options = optparse.Values()
    requests = git_cl._make_tryjob_schedule_requests(
        changelist, jobs, options, patchset=None)

    # requestId is non-deterministic. Just assert that it's there and has
    # a particular length.
    self.assertEqual(len(requests[0]['scheduleBuild'].pop('requestId')), 36)
    self.assertEqual(requests, [{
        'scheduleBuild': {
            'builder': {
                'bucket': 'try',
                'builder': 'my-builder',
                'project': 'chromium'
            },
            'gerritChanges': [self.exampleGerritChange()],
            'properties': {
                'category': 'git_cl_try'
            },
            'tags': [{
                'key': 'builder',
                'value': 'my-builder'
            }, {
                'key': 'user_agent',
                'value': 'git_cl_try'
            }]
        }
    }])

  def testMakeRequestsHelperPresubmitSetsDryRunProperty(self):
    changelist = ChangelistMock(gerrit_change=self.exampleGerritChange())
    jobs = [('chromium', 'try', 'presubmit')]
    options = optparse.Values()
    requests = git_cl._make_tryjob_schedule_requests(
        changelist, jobs, options, patchset=None)
    self.assertEqual(requests[0]['scheduleBuild']['properties'], {
        'category': 'git_cl_try',
        'dry_run': 'true'
    })

  def testMakeRequestsHelperRevisionSet(self):
    # Gitiles commit is specified when revision is in options.
    changelist = ChangelistMock(gerrit_change=self.exampleGerritChange())
    jobs = [('chromium', 'try', 'my-builder')]
    options = optparse.Values({'revision': 'ba5eba11'})
    requests = git_cl._make_tryjob_schedule_requests(
        changelist, jobs, options, patchset=None)
    self.assertEqual(
        requests[0]['scheduleBuild']['gitilesCommit'], {
            'host': 'chromium-review.googlesource.com',
            'id': 'ba5eba11',
            'project': 'depot_tools'
        })

  def testMakeRequestsHelperRetryFailedSet(self):
    # An extra tag is added when retry_failed is in options.
    changelist = ChangelistMock(gerrit_change=self.exampleGerritChange())
    jobs = [('chromium', 'try', 'my-builder')]
    options = optparse.Values({'retry_failed': 'true'})
    requests = git_cl._make_tryjob_schedule_requests(
        changelist, jobs, options, patchset=None)
    self.assertEqual(
        requests[0]['scheduleBuild']['tags'], [
            {
                'key': 'builder',
                'value': 'my-builder'
            },
            {
                'key': 'user_agent',
                'value': 'git_cl_try'
            },
            {
                'key': 'retry_failed',
                'value': '1'
            }
        ])

  def testMakeRequestsHelperCategorySet(self):
    # The category property can be overridden with options.
    changelist = ChangelistMock(gerrit_change=self.exampleGerritChange())
    jobs = [('chromium', 'try', 'my-builder')]
    options = optparse.Values({'category': 'my-special-category'})
    requests = git_cl._make_tryjob_schedule_requests(
        changelist, jobs, options, patchset=None)
    self.assertEqual(requests[0]['scheduleBuild']['properties'],
                     {'category': 'my-special-category'})


class CMDFormatTestCase(unittest.TestCase):

  def setUp(self):
    super(CMDFormatTestCase, self).setUp()
    mock.patch('git_cl.RunCommand').start()
    mock.patch('clang_format.FindClangFormatToolInChromiumTree').start()
    mock.patch('clang_format.FindClangFormatScriptInChromiumTree').start()
    mock.patch('git_cl.settings').start()
    self._top_dir = tempfile.mkdtemp()
    self.addCleanup(mock.patch.stopall)

  def tearDown(self):
    shutil.rmtree(self._top_dir)
    super(CMDFormatTestCase, self).tearDown()

  def _make_temp_file(self, fname, contents):
    with open(os.path.join(self._top_dir, fname), 'w') as tf:
      tf.write('\n'.join(contents))

  def _make_yapfignore(self, contents):
    self._make_temp_file('.yapfignore', contents)

  def _check_yapf_filtering(self, files, expected):
    self.assertEqual(expected, git_cl._FilterYapfIgnoredFiles(
        files, git_cl._GetYapfIgnorePatterns(self._top_dir)))

  def _run_command_mock(self, return_value):
    def f(*args, **kwargs):
      if 'stdin' in kwargs:
        self.assertIsInstance(kwargs['stdin'], bytes)
      return return_value
    return f

  def testClangFormatDiffFull(self):
    self._make_temp_file('test.cc', ['// test'])
    git_cl.settings.GetFormatFullByDefault.return_value = False
    diff_file = [os.path.join(self._top_dir, 'test.cc')]
    mock_opts = mock.Mock(full=True, dry_run=True, diff=False)

    # Diff
    git_cl.RunCommand.side_effect = self._run_command_mock('  // test')
    return_value = git_cl._RunClangFormatDiff(mock_opts, diff_file,
                                              self._top_dir, 'HEAD')
    self.assertEqual(2, return_value)

    # No diff
    git_cl.RunCommand.side_effect = self._run_command_mock('// test')
    return_value = git_cl._RunClangFormatDiff(mock_opts, diff_file,
                                              self._top_dir, 'HEAD')
    self.assertEqual(0, return_value)

  def testClangFormatDiff(self):
    git_cl.settings.GetFormatFullByDefault.return_value = False
    # A valid file is required, so use this test.
    clang_format.FindClangFormatToolInChromiumTree.return_value = __file__
    mock_opts = mock.Mock(full=False, dry_run=True, diff=False)

    # Diff
    git_cl.RunCommand.side_effect = self._run_command_mock('error')
    return_value = git_cl._RunClangFormatDiff(
        mock_opts, ['.'], self._top_dir, 'HEAD')
    self.assertEqual(2, return_value)

    # No diff
    git_cl.RunCommand.side_effect = self._run_command_mock('')
    return_value = git_cl._RunClangFormatDiff(mock_opts, ['.'], self._top_dir,
                                              'HEAD')
    self.assertEqual(0, return_value)

  def testYapfignoreExplicit(self):
    self._make_yapfignore(['foo/bar.py', 'foo/bar/baz.py'])
    files = [
      'bar.py',
      'foo/bar.py',
      'foo/baz.py',
      'foo/bar/baz.py',
      'foo/bar/foobar.py',
    ]
    expected = [
      'bar.py',
      'foo/baz.py',
      'foo/bar/foobar.py',
    ]
    self._check_yapf_filtering(files, expected)

  def testYapfignoreSingleWildcards(self):
    self._make_yapfignore(['*bar.py', 'foo*', 'baz*.py'])
    files = [
      'bar.py',       # Matched by *bar.py.
      'bar.txt',
      'foobar.py',    # Matched by *bar.py, foo*.
      'foobar.txt',   # Matched by foo*.
      'bazbar.py',    # Matched by *bar.py, baz*.py.
      'bazbar.txt',
      'foo/baz.txt',  # Matched by foo*.
      'bar/bar.py',   # Matched by *bar.py.
      'baz/foo.py',   # Matched by baz*.py, foo*.
      'baz/foo.txt',
    ]
    expected = [
      'bar.txt',
      'bazbar.txt',
      'baz/foo.txt',
    ]
    self._check_yapf_filtering(files, expected)

  def testYapfignoreMultiplewildcards(self):
    self._make_yapfignore(['*bar*', '*foo*baz.txt'])
    files = [
      'bar.py',       # Matched by *bar*.
      'bar.txt',      # Matched by *bar*.
      'abar.py',      # Matched by *bar*.
      'foobaz.txt',   # Matched by *foo*baz.txt.
      'foobaz.py',
      'afoobaz.txt',  # Matched by *foo*baz.txt.
    ]
    expected = [
      'foobaz.py',
    ]
    self._check_yapf_filtering(files, expected)

  def testYapfignoreComments(self):
    self._make_yapfignore(['test.py', '#test2.py'])
    files = [
      'test.py',
      'test2.py',
    ]
    expected = [
      'test2.py',
    ]
    self._check_yapf_filtering(files, expected)

  def testYapfignoreBlankLines(self):
    self._make_yapfignore(['test.py', '', '', 'test2.py'])
    files = [
      'test.py',
      'test2.py',
      'test3.py',
    ]
    expected = [
      'test3.py',
    ]
    self._check_yapf_filtering(files, expected)

  def testYapfignoreWhitespace(self):
    self._make_yapfignore([' test.py '])
    files = [
      'test.py',
      'test2.py',
    ]
    expected = [
      'test2.py',
    ]
    self._check_yapf_filtering(files, expected)

  def testYapfignoreNoFiles(self):
    self._make_yapfignore(['test.py'])
    self._check_yapf_filtering([], [])

  def testYapfignoreMissingYapfignore(self):
    files = [
      'test.py',
    ]
    expected = [
      'test.py',
    ]
    self._check_yapf_filtering(files, expected)


if __name__ == '__main__':
  logging.basicConfig(
      level=logging.DEBUG if '-v' in sys.argv else logging.ERROR)
  unittest.main()
