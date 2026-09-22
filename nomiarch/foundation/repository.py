"""Customer GitHub proposals and current-head human review verification.

No merge, review, deployment or policy-edit operation is exposed to the proposer.
The token is held by the controller, never written into a scaffold or sent to Core.
"""
import base64
import hashlib
import json
import re
import urllib.error
import urllib.request

from nomiarch.bootstrap.config import require
from nomiarch.common import NomiarchError, canonical
from . import scaffold


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise NomiarchError('Repository redirect refused; confirm the current repository name')


class GitHub:
    def __init__(self, token):
        require(isinstance(token, str) and 10 <= len(token) <= 512 and not any(c.isspace() for c in token), 'Enter a valid customer GitHub token')
        self.token = token

    def call(self, path, data=None, method=None):
        require(path.startswith('/') and not path.startswith('//'), 'Invalid GitHub API path')
        headers = {'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json',
                   'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'Nomiarch-Setup', 'Content-Type': 'application/json'}
        req = urllib.request.Request('https://api.github.com' + path, data=canonical(data) if data is not None else None,
                                     headers=headers, method=method)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        try:
            with opener.open(req, timeout=45) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
                require(len(raw) <= 4 * 1024 * 1024, 'Repository response is too large')
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            # Server responses may echo credentials/input. Keep these out of UI logs.
            raise NomiarchError(f'GitHub returned HTTP {e.code}. Check repository access, permissions and branch protection.') from None

    def pages(self, path):
        values = []
        for page in range(1, 11):
            result = self.call(path + ('&' if '?' in path else '?') + f'per_page=100&page={page}')
            require(isinstance(result, list), 'Unexpected repository response')
            values.extend(result)
            if len(result) < 100: return values
        raise NomiarchError('Repository history exceeds this preview’s review limit')

    @staticmethod
    def root(value):
        r = value['repository']
        return '/repos/' + r['owner'] + '/' + r['name']

    def protect(self, value):
        # Only the separate human setup flow calls this administration operation.
        root = self.root(value)
        self.call(root + '/branches/main/protection', {
            'required_status_checks': None, 'enforce_admins': True,
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

    def main_head(self, value):
        self.assert_protection(value)
        return self.call(self.root(value) + '/git/ref/heads/main')['object']['sha']

    def create_repository(self, value):
        value = scaffold.validate_project(value)
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
        return created['html_url']

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
        return {'url': pull['html_url'], 'number': pull['number'], 'head': created['sha'], 'base': base}

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
        return {'repository': root[len('/repos/'):], 'pull_request': number, 'head': head, 'merge': current, 'human_reviewers': sorted(accepted)}
