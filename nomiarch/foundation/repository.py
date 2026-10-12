"""Customer GitHub proposals and current-head human review verification.

No merge, review, deployment or policy-edit operation is exposed to the proposer.
The token is held by the controller, never written into a scaffold or sent to Core.
"""
import base64
import copy
import hashlib
import json
import re
import ssl
import urllib.error
import urllib.request

from nomiarch.bootstrap.config import require
from nomiarch.common import NomiarchError, canonical
from . import scaffold
from .repository_transport import PrivateHTTPSHandler, certificate_context


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fp.close()
        raise NomiarchError('Repository redirect refused; confirm the current repository name')


class GitHub:
    def __init__(self, token, repository=None):
        require(isinstance(token, str) and 10 <= len(token) <= 512 and all(33 <= ord(c) <= 126 for c in token), 'Enter a valid customer GitHub token')
        self.token = token
        self.repository = copy.deepcopy(repository)
        if repository is not None:
            scaffold.validate_repository(repository, 'organisation', 'local-isolated')
            require(repository['mode'] in scaffold.REVIEW_MODES, 'Choose a supported review repository')
        internal = repository is not None and repository['mode'] == 'github-enterprise'
        self.origin = repository['server_url'] if internal else 'https://github.com'
        self.api = self.origin + '/api/v3' if internal else 'https://api.github.com'
        context = certificate_context(repository.get('ca_certificate') if internal else None)
        handler = PrivateHTTPSHandler(context=context) if internal else urllib.request.HTTPSHandler(context=context)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(), handler)

    def call(self, path, data=None, method=None):
        require(path.startswith('/') and not path.startswith('//') and not any(c in path for c in ('\\', '#', '\r', '\n')), 'Invalid GitHub API path')
        headers = {'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json',
                   'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'Nomiarch-Setup', 'Content-Type': 'application/json'}
        req = urllib.request.Request(self.api + path, data=canonical(data) if data is not None else None,
                                     headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=45) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
                require(len(raw) <= 4 * 1024 * 1024, 'Repository response is too large')
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            # Server responses may echo credentials/input. Keep these out of UI logs.
            e.close()
            raise NomiarchError(f'GitHub returned HTTP {e.code}. Check repository access, permissions and branch protection.') from None
        except (urllib.error.URLError, OSError, ssl.SSLError):
            raise NomiarchError('Cannot establish verified HTTPS to the repository. Check its address, private route and trusted public CA certificate.') from None
        except (ValueError, UnicodeError):
            raise NomiarchError('The repository returned an invalid response') from None

    def pages(self, path):
        values = []
        for page in range(1, 11):
            result = self.call(path + ('&' if '?' in path else '?') + f'per_page=100&page={page}')
            require(isinstance(result, list), 'Unexpected repository response')
            values.extend(result)
            if len(result) < 100: return values
        raise NomiarchError('Repository history exceeds this preview’s review limit')

    def root(self, value):
        r = value['repository']
        require(r['mode'] in scaffold.REVIEW_MODES, 'This configuration has no supported review repository')
        if self.repository is None:
            require(r['mode'] == 'github', 'Reconnect using the internal repository settings before sending credentials')
        else:
            require(r == self.repository, 'Repository settings changed; reconnect and re-enter the token for this configuration')
        return '/repos/' + r['owner'] + '/' + r['name']

    def repository_url(self, value):
        return self.origin + self.root(value)[len('/repos'):]

    def required_checks(self, value):
        """Infrastructure recipes require the trusted validation check on the exact head commit."""
        if not scaffold.is_infrastructure(value):
            return None  # Core-era recipes keep their original protection contract.
        check = {'context': scaffold.VALIDATION_CHECK}
        if value['repository']['mode'] == 'github':
            check['app_id'] = scaffold.GITHUB_ACTIONS_APP_ID
        return {'strict': True, 'checks': [check]}

    def protect(self, value):
        # Only the separate human setup flow calls this administration operation.
        root = self.root(value)
        self.call(root + '/branches/main/protection', {
            'required_status_checks': self.required_checks(value), 'enforce_admins': True,
            'required_pull_request_reviews': {'dismiss_stale_reviews': True, 'require_code_owner_reviews': True,
                                             'required_approving_review_count': 1, 'require_last_push_approval': True},
            'restrictions': None, 'allow_force_pushes': False, 'allow_deletions': False,
            'required_conversation_resolution': True}, 'PUT')

    def assert_protection(self, value):
        p = self.call(self.root(value) + '/branches/main/protection')
        review = p.get('required_pull_request_reviews') or {}
        require(p.get('enforce_admins', {}).get('enabled') and review.get('dismiss_stale_reviews')
                and review.get('require_code_owner_reviews') and review.get('required_approving_review_count', 0) >= 1
                and review.get('require_last_push_approval') and not p.get('allow_force_pushes', {}).get('enabled')
                and not p.get('allow_deletions', {}).get('enabled'), 'Required human branch protection is missing or weakened')
        required = self.required_checks(value)
        if required:
            status = p.get('required_status_checks') or {}
            checks = {c.get('context'): c for c in status.get('checks') or []} if status.get('checks') is not None else {c: {} for c in status.get('contexts') or []}
            expected = required['checks'][0]
            require(status.get('strict') is True and expected['context'] in checks
                    and ('app_id' not in expected or checks[expected['context']].get('app_id') == expected['app_id']),
                    'The required trusted validation check is missing or weakened on main')

    def main_head(self, value):
        self.assert_protection(value)
        return self.call(self.root(value) + '/git/ref/heads/main')['object']['sha']

    def create_repository(self, value):
        value = scaffold.validate_project(value)
        self.root(value)
        r = value['repository']
        who = self.call('/user')
        path = '/user/repos' if who['login'].lower() == r['owner'].lower() else '/orgs/' + r['owner'] + '/repos'
        created = self.call(path, {'name': r['name'], 'private': True, 'auto_init': True,
                                  'description': 'Customer-owned Nomiarch configuration and human approvals'})
        require(created.get('private') is True, 'The customer configuration repository must be private')
        # GitHub's auto-initialized default may be customised by the organisation.
        if created['default_branch'] != 'main':
            self.call(self.root(value) + '/branches/' + created['default_branch'] + '/rename', {'new_name': 'main'})
        self.protect(value)
        return self.repository_url(value)

    def propose(self, value, files, title, description):
        value = scaffold.validate_project(value)
        root = self.root(value)
        self.assert_protection(value)
        repository = self.call(root)
        require(repository.get('private') is True and repository.get('default_branch') == 'main', 'Choose a private customer repository with main as its default branch')
        base = self.call(root + '/git/ref/heads/main')['object']['sha']
        commit = self.call(root + '/git/commits/' + base)
        existing = self.call(root + '/git/trees/' + commit['tree']['sha'] + '?recursive=1')
        require(not existing.get('truncated'), 'Repository tree exceeds the supported size')
        names = {item['path'] for item in existing['tree'] if item['type'] == 'blob'}
        # Never silently replace unrelated customer code. Foundation repositories
        # are dedicated; only the initial GitHub README is admitted as a seed.
        requests = {'requests/reconcile.json', 'requests/maintenance.json'}
        require(names <= set(files) | requests or names <= {'README.md'}, 'Repository contains files outside the selected scaffold; review them separately')
        if 'foundation.json' in names:
            entry = next(x for x in existing['tree'] if x['path'] == 'foundation.json')
            blob = self.call(root + '/git/blobs/' + entry['sha'])
            previous = scaffold.validate_project(json.loads(base64.b64decode(blob['content'])))
            require(previous['id'] == value['id'], 'This repository belongs to a different foundation')
        entries = [{'path': name, 'mode': '100644', 'type': 'blob', 'content': text} for name, text in sorted(files.items())]
        entries += [{'path': name, 'mode': '100644', 'type': 'blob', 'sha': None} for name in sorted((names & requests) - set(files))]
        tree = self.call(root + '/git/trees', {'base_tree': commit['tree']['sha'], 'tree': entries})
        created = self.call(root + '/git/commits', {'message': title, 'tree': tree['sha'], 'parents': [base]})
        branch = 'nomiarch/proposal-' + created['sha'][:16]
        self.call(root + '/git/refs', {'ref': 'refs/heads/' + branch, 'sha': created['sha']})
        pull = self.call(root + '/pulls', {'title': title, 'head': branch, 'base': 'main', 'body': description,
                                         'maintainer_can_modify': False})
        require(type(pull.get('number')) is int and pull['number'] > 0, 'Invalid repository pull request number')
        return {'url': self.repository_url(value) + '/pull/' + str(pull['number']), 'number': pull['number'], 'head': created['sha'], 'base': base}

    def approved(self, value, snapshot, number):
        require(type(number) is int and number > 0, 'Enter the reviewed pull request number')
        root = self.root(value)
        self.assert_protection(value)
        pull = self.call(root + f'/pulls/{number}')
        require(pull.get('merged') and pull['base']['ref'] == 'main' and pull['base']['repo']['full_name'].lower() == root[len('/repos/'):].lower(),
                'Merge the human-approved change into the customer main branch first')
        require(pull['head']['repo'] and pull['head']['repo']['full_name'].lower() == root[len('/repos/'):].lower(), 'Fork proposals are not admitted by this recipe')
        head = pull['head']['sha']
        current = self.call(root + '/git/ref/heads/main')['object']['sha']
        require(current == pull['merge_commit_sha'], 'The reviewed change is no longer the current main commit; review and plan the latest configuration')
        commits = self.call(root + '/git/commits/' + current)
        tree = self.call(root + '/git/trees/' + commits['tree']['sha'] + '?recursive=1')
        require(not tree.get('truncated'), 'Repository tree is incomplete')
        blobs = {x['path']: x for x in tree['tree'] if x['type'] != 'tree'}
        require(set(blobs) == set(snapshot['files']), 'Repository files do not match the configuration you planned')
        for name, expected in snapshot['files'].items():
            require(blobs[name]['type'] == 'blob' and blobs[name]['mode'] == '100644', 'Unexpected executable or link in customer repository')
            item = self.call(root + '/git/blobs/' + blobs[name]['sha'])
            require(item.get('encoding') == 'base64', 'Unsupported repository encoding')
            actual = hashlib.sha256(base64.b64decode(item['content'])).hexdigest()
            require(actual == expected, 'Repository content differs from the plan: ' + name)
        reviews = self.pages(root + f'/pulls/{number}/reviews')
        latest = {}
        for review in reviews:
            login = review['user']['login'].lower()
            if review['state'] in {'APPROVED', 'CHANGES_REQUESTED', 'DISMISSED'}:
                latest[login] = review
        allowed = {x.lower() for x in value['repository']['approvers']}
        accepted = [login for login, r in latest.items() if login in allowed and r['state'] == 'APPROVED'
                    and r.get('commit_id') == head and r['user'].get('type') == 'User'
                    and login != pull['user']['login'].lower()]
        require(accepted, 'A designated human other than the proposer must approve the exact final PR commit')
        require(not any(r['state'] == 'CHANGES_REQUESTED' for login, r in latest.items() if login in allowed), 'A designated reviewer still requests changes')
        proof = {'server': self.origin, 'repository': root[len('/repos/'):], 'pull_request': number, 'head': head, 'merge': current, 'human_reviewers': sorted(accepted)}
        if scaffold.is_infrastructure(value):
            proof['validation'] = self.validated(value, root, head)
        return proof

    def validated(self, value, root, head):
        """The newest trusted validation run for the exact reviewed commit must have succeeded.

        Selection uses the check-run id, which GitHub assigns in creation order (a rerun
        or re-trigger always gets a new, larger id), never completion time, so a newer
        queued or in-progress run can't be hidden behind an older success. `filter=all`
        disables GitHub's default "latest only" filtering and every page is read; an
        incomplete listing fails closed. Only check runs created by the GitHub Actions
        app for this head SHA count. The merged tree is separately compared with the
        admitted recipe, so an edited workflow or validator can't stand in for it.
        """
        path = root + '/commits/' + head + '/check-runs?check_name=' + scaffold.VALIDATION_CHECK + '&filter=all&per_page=100'
        runs, total = [], None
        for page in range(1, 11):
            result = self.call(path + f'&page={page}')
            require(isinstance(result, dict) and isinstance(result.get('check_runs'), list) and type(result.get('total_count')) is int,
                    'Unexpected check-run response from the repository')
            total = result['total_count'] if total is None else total
            runs.extend(result['check_runs'])
            if len(result['check_runs']) < 100 or len(runs) >= total:
                break
        require(len(runs) >= total, 'The repository returned an incomplete check-run listing; refusing to trust a partial history')
        trusted = []
        for run in runs:
            app = run.get('app') or {}
            if run.get('name') != scaffold.VALIDATION_CHECK or run.get('head_sha') != head or app.get('slug') != 'github-actions':
                continue
            if value['repository']['mode'] == 'github' and app.get('id') != scaffold.GITHUB_ACTIONS_APP_ID:
                continue
            require(type(run.get('id')) is int and run['id'] > 0, 'A check run without a valid identity was returned')
            trusted.append(run)
        require(trusted, 'The trusted ' + scaffold.VALIDATION_CHECK + ' check has not run for the reviewed commit; a check for another commit or from another source is not accepted')
        latest = max(trusted, key=lambda r: r['id'])
        require(latest.get('status') == 'completed',
                'The newest trusted validation run for the reviewed commit is ' + str(latest.get('status')) + '; wait for it to finish before planning or applying')
        require(latest.get('conclusion') == 'success',
                'The newest trusted validation run for the reviewed commit did not succeed: ' + str(latest.get('conclusion')))
        return {'check': scaffold.VALIDATION_CHECK, 'app': 'github-actions', 'app_id': (latest.get('app') or {}).get('id'),
                'head_sha': head, 'completed_at': latest.get('completed_at'), 'check_run_id': latest['id'], 'url': latest.get('html_url'),
                'runs_considered': len(trusted)}
