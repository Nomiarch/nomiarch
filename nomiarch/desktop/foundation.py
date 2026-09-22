"""Customer setup screens. Tk values are captured before starting background work."""
import json
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser

from nomiarch.common import NomiarchError, read_json, write_json
from nomiarch.foundation import changes, deployment, maintenance, observe, scaffold
from nomiarch.foundation.repository import GitHub
from . import cloud, service
from .catalog import PINS


class FoundationScreens:
    def foundation_variables(self):
        for name, value in {'organisation': 'my-organisation', 'environment': 'evaluation', 'usage': 'personal',
                            'repo_mode': 'local', 'repo_owner': '', 'repo_name': 'nomiarch-environment',
                            'approvers': '', 'github_token': '', 'pull_number': '', 'isolation': 'local-isolated',
                            'project_folder': '', 'subscription': '', 'region': 'canadacentral',
                            'vm_size': 'Standard_D2s_v5', 'admin_cidr': '', 'subnet': ''}.items():
            setattr(self, name, tk.StringVar(value=value))
        self.target, self.project_path, self.azure_image = 'local', None, None
        self.accounts, self.subnets = [], []

    def customer(self, target):
        self.target, self.project_path = target, None
        self.isolation.set('local-isolated' if target == 'local' else 'cloud-restricted')
        self.clear('Who is this system for?', 'Your choices create a configuration folder you own. Future changes can be reviewed against this starting point.', '01  Your environment')
        for label, var in [('Organisation or project', self.organisation), ('Environment', self.environment)]: self.entry(label, var)
        ttk.Radiobutton(self.content, text='Personal evaluation — I review and approve on this computer', variable=self.usage, value='personal').pack(anchor='w', pady=8)
        ttk.Radiobutton(self.content, text='Organisation — a designated person reviews changes in our repository', variable=self.usage, value='organisation').pack(anchor='w', pady=8)
        if target == 'local':
            ttk.Radiobutton(self.content, text='Isolated VM — block new outbound connections from the VM', variable=self.isolation, value='local-isolated').pack(anchor='w', pady=8)
            ttk.Radiobutton(self.content, text='Disconnected site — use files already admitted into this environment', variable=self.isolation, value='disconnected').pack(anchor='w', pady=8)
        self.button('Continue', self.repository_screen, True)
        self.button('Back', self.home)

    def repository_screen(self):
        try:
            scaffold.slug(self.organisation.get(), 'Organisation'); scaffold.slug(self.environment.get(), 'Environment')
        except Exception as e: messagebox.showerror('Check the names', str(e)); return
        if self.usage.get() == 'organisation' and self.repo_mode.get() == 'local': self.repo_mode.set('github')
        if self.isolation.get() == 'disconnected':
            self.mode.set('offline')
            self.repo_mode.set('offline' if self.usage.get() == 'organisation' else 'local')
        if not self.project_folder.get(): self.project_folder.set(str(self.root_dir / 'projects' / (self.organisation.get() + '-' + self.environment.get())))
        self.clear('Keep control of your configuration', 'The folder contains your settings and the reviewed recipe. Credentials, deployment plans and state are stored separately.', '02  Customer configuration')
        self.entry('New configuration folder', self.project_folder)
        self.button('Choose parent folder…', self.choose_project_parent)
        if self.usage.get() == 'personal':
            ttk.Radiobutton(self.content, text='Keep a local configuration and approval record', variable=self.repo_mode, value='local').pack(anchor='w', pady=5)
        if self.isolation.get() != 'disconnected':
            ttk.Radiobutton(self.content, text='Private GitHub repository with human review', variable=self.repo_mode, value='github').pack(anchor='w', pady=5)
        ttk.Radiobutton(self.content, text='Export for an internal repository (deployment integration not included yet)', variable=self.repo_mode, value='offline').pack(anchor='w', pady=5)
        self.button('Continue', self.repository_next, True)
        self.button('Back', lambda: self.customer(self.target))

    def choose_project_parent(self):
        path = filedialog.askdirectory(title='Choose where to keep your customer configuration')
        if path: self.project_folder.set(str(Path(path) / (self.organisation.get() + '-' + self.environment.get())))

    def repository_next(self):
        if self.repo_mode.get() == 'github': self.github_screen()
        elif self.target == 'azure': self.cloud_screen()
        else: self.prerequisites()

    def github_screen(self):
        self.clear('Your organisation’s repository', 'A designated human must approve the final pull request. The proposer cannot approve their own change. Use a separate customer account for deployment.', '02  Repository access')
        for label, var in [('GitHub owner', self.repo_owner), ('Repository name', self.repo_name), ('Human reviewer logins', self.approvers)]: self.entry(label, var)
        row = ttk.Frame(self.content); row.pack(fill='x', pady=6)
        ttk.Label(row, text='GitHub token', width=24).pack(side='left')
        ttk.Entry(row, textvariable=self.github_token, show='•').pack(side='left', fill='x', expand=True)
        ttk.Label(self.content, text='The token stays in this app’s memory. Repository creation needs administration access; later proposal access can be narrower. Your GitHub plan must support protected private branches.', wraplength=820).pack(anchor='w', pady=8)
        self.button('Repository access guide', lambda: webbrowser.open('https://nomiarch.com/docs/running/#customer-repository'))
        self.button('Continue', self.cloud_screen if self.target == 'azure' else self.prerequisites, True)
        self.button('Back', self.repository_screen)

    def cloud_screen(self):
        self.target = 'azure'
        self.clear('Set up in your cloud', 'Azure is the first cloud recipe. Sign in with Microsoft in your browser. Nomiarch receives no account password. Your organisation must provide a private route to the selected subnet.', '03  Cloud destination')
        self.button('Sign in to Microsoft Azure', self.cloud_login, True)
        self.button('Open Microsoft sign-in page', lambda: webbrowser.open('https://microsoft.com/devicelogin'))
        ttk.Label(self.content, text='A sign-in code appears below when Microsoft requests it.\nThe wizard uses a separate Azure profile on this computer.', wraplength=820).pack(anchor='w', pady=12)
        self.button('Back', self.repository_screen)

    def cloud_login(self):
        def ready(accounts):
            if not accounts: raise NomiarchError('This Microsoft account has no enabled Azure subscriptions')
            self.accounts = accounts
            self.cloud_account()
        self.start(lambda: cloud.sign_in(self.root_dir, self.repo, self.download_progress,
                                         lambda message: self.events.put(('device-code', message))), ready)

    def cloud_account(self):
        self.clear('Choose an Azure subscription', 'Nothing is created during this check. The selected image version and resources will be fixed in the configuration for review.', '03  Cloud destination')
        box = ttk.Combobox(self.content, textvariable=self.subscription, state='readonly', width=80,
                           values=[a['name'] + ' · ' + a['id'] for a in self.accounts])
        box.pack(anchor='w', pady=10); box.current(0)
        self.entry('Azure region', self.region)
        def check():
            selected = self.accounts[box.current()]['id']; location = self.region.get().strip()
            self.azure_subscription = selected
            self.start(lambda: cloud.account_details(selected, location), self.cloud_network)
        self.button('Find my private networks', check, True)
        self.button('Back', self.cloud_screen)

    def cloud_network(self, result):
        self.azure_image, self.subnets = result['image'], result['subnets']
        self.clear('Private access to your system', 'Choose a subnet reachable through your organisation’s VPN or private network. The VM has no public IP. Azure platform dependencies remain; this is cloud isolation.', '03  Private network')
        if not self.subnets:
            ttk.Label(self.content, text='No eligible subnet was found in this region. Your cloud administrator must create a private administration route before this preview can install Core.', wraplength=820).pack(anchor='w', pady=10)
            self.button('Choose another region', self.cloud_account)
            self.button('Home', self.home)
            return
        box = ttk.Combobox(self.content, textvariable=self.subnet, state='readonly', width=80, values=[s['label'] for s in self.subnets])
        box.pack(anchor='w', pady=10); box.current(0)
        self.entry('Management CIDR', self.admin_cidr)
        self.entry('Azure VM size', self.vm_size)
        ttk.Label(self.content, text='Ask your administrator for the management range (for example, 10.20.30.0/24). Only that range can open SSH administration sessions.', wraplength=820).pack(anchor='w', pady=10)
        def next_step():
            self.azure_subnet = self.subnets[box.current()]['id']
            self.download_screen(False)
        self.button('Continue to system files', next_step, True)
        self.button('Back', self.cloud_account)

    def configuration(self):
        arch = 'amd64' if self.target == 'azure' else service.architecture()
        config = {'api_version': 'nomiarch.io/v1alpha1', 'name': self.name.get(), 'target': self.target, 'architecture': arch,
                  'capacity': {'cpus': int(self.cpus.get()), 'memory_gib': int(self.memory.get()), 'disk_gib': int(self.disk.get())},
                  'lifecycle': {'destroy_after': False, 'max_runtime_minutes': 120}}
        if self.target == 'local':
            config['local'] = {'image': self.paths['image'], 'image_sha256': PINS[arch]['image'][1]}
        else:
            config['azure'] = {'subscription_id': self.azure_subscription, 'location': self.region.get(), 'vm_size': self.vm_size.get(),
                               'ssh_user': 'nomiarch', 'ssh_key': '@controller', 'ssh_public_key': '@controller',
                               'admin_cidr': self.admin_cidr.get().strip(), 'subnet_id': self.azure_subnet, 'image': self.azure_image}
        repository = {'mode': self.repo_mode.get()}
        if repository['mode'] == 'github':
            repository.update(owner=self.repo_owner.get().strip(), name=self.repo_name.get().strip(),
                              approvers=[a.strip().lstrip('@') for a in self.approvers.get().split(',') if a.strip()])
        return scaffold.project(config, self.organisation.get(), self.environment.get(), self.usage.get(), self.isolation.get(), repository)

    def make_scaffold(self):
        try: value, folder = self.configuration(), self.project_folder.get()
        except Exception as e: messagebox.showerror('Check the settings', str(e)); return
        def ready(path):
            self.project_path = path
            self.scaffold_ready()
        self.start(lambda: scaffold.create(folder, value, self.repo), ready)

    def scaffold_ready(self):
        value, _ = scaffold.load_supported(self.project_path, self.repo)
        self.clear('Your configuration is ready', 'Review the folder before planning. Preparing a plan checks prerequisites and proposed resources; it does not create the VM.', '04  Review configuration')
        ttk.Label(self.content, text=str(self.project_path), wraplength=820).pack(anchor='w', pady=8)
        self.button('Open configuration folder', lambda: service.open_folder(self.project_path))
        mode = value['repository']['mode']
        if mode == 'github':
            row = ttk.Frame(self.content); row.pack(fill='x', pady=4)
            ttk.Label(row, text='GitHub token (not saved)', width=24).pack(side='left')
            ttk.Entry(row, textvariable=self.github_token, show='•').pack(side='left', fill='x', expand=True)
            self.button('Create a NEW private customer repository', self.create_customer_repo)
            self.button('Open a configuration pull request', self.open_initial_pr)
            self.entry('Approved, merged PR number', self.pull_number)
            self.button('Prepare deployment plan', self.prepare_foundation, True)
        elif mode == 'offline':
            ttk.Label(self.content, text='Export complete. Copy this folder into your internal review system. This preview cannot verify internal repository approvals, so organisation deployment stops here.', wraplength=820).pack(anchor='w', pady=12)
        else:
            self.button('Prepare deployment plan', self.prepare_foundation, True)
        self.button('Home', self.home)

    def create_customer_repo(self):
        if not messagebox.askokcancel('Create customer repository', 'Create a new private repository in the named customer account and require human reviews on main? An existing repository will not be overwritten.'): return
        value, _ = scaffold.load_supported(self.project_path, self.repo)
        token = self.github_token.get()
        self.start(lambda: GitHub(token).create_repository(value), lambda url: (self.status.set('Private repository created with required human review.'), webbrowser.open(url)))

    def open_initial_pr(self):
        value, _ = scaffold.load_supported(self.project_path, self.repo)
        files, token = scaffold.render(value, self.repo), self.github_token.get()
        if not messagebox.askokcancel('Propose configuration', 'Push these configuration files and open a pull request in your customer repository for a designated human to review?'): return
        def ready(result):
            self.pull_number.set(str(result['number']))
            self.status.set('Have a designated human review and merge this PR, then prepare the deployment plan.')
            webbrowser.open(result['url'])
        self.start(lambda: GitHub(token).propose(value, files, 'Create Nomiarch foundation',
                    'Review the customer settings, versioned recipe and approval/network policies. This PR creates no cloud or VM resources. Deployment requires a separate exact-plan approval in Nomiarch Setup.'), ready)

    def repository_check(self):
        value, _ = scaffold.load_supported(self.project_path, self.repo)
        if value['repository']['mode'] != 'github': return None
        token, number = self.github_token.get(), int(self.pull_number.get())
        client = GitHub(token)
        return lambda project, snapshot: client.approved(project, snapshot, number)

    def prepare_foundation(self):
        try: check = self.repository_check()
        except Exception as e: messagebox.showerror('Repository approval', str(e)); return
        folder, paths = Path(self.project_path), dict(self.paths)
        def task():
            value, snapshot = scaffold.load_supported(folder, self.repo)
            if check: check(value, snapshot)
            if value['configuration']['target'] == 'local': service.preflight()
            else: cloud.tools_ready(self.root_dir, self.repo, self.download_progress)
            return deployment.prepare(folder, self.state_dir, self.repo, paths)
        self.start(task, self.show_foundation_plan)

    def show_foundation_plan(self, result):
        self.pending_foundation = result
        record = result['plan']
        self.clear('Review what will be created', 'Approval applies only to this plan. A changed setting, release file or plan requires a fresh review. Plans expire after one hour.', '05  Human approval')
        summary = record['summary']; capacity = summary['capacity']
        lines = [('System', summary['system']), ('Location', 'This computer' if summary['destination'] == 'local' else 'Microsoft Azure'),
                 ('Organisation / environment', summary['organisation'] + ' / ' + summary['environment']),
                 ('Size', f"{capacity['cpus']} CPUs · {capacity['memory_gib']} GB memory · {capacity['disk_gib']} GB disk"),
                 ('Network', summary['network']), ('Resources', summary['charges'])]
        for label, value in lines:
            ttk.Label(self.content, text=label + ': ' + value, wraplength=820).pack(anchor='w', pady=5)
        if summary.get('resources'):
            azure = summary['azure']
            ttk.Label(self.content, text='Azure: ' + azure['subscription_id'] + ' · ' + azure['location'], wraplength=820).pack(anchor='w', pady=4)
            ttk.Label(self.content, text='Resource group: ' + summary['resource_group'], wraplength=820).pack(anchor='w', pady=4)
            ttk.Label(self.content, text=f"The saved Terraform plan lists {len(summary['resources'])} resource actions.").pack(anchor='w', pady=4)
        ttk.Label(self.content, text='Plan reference: ' + record['digest'][:16]).pack(anchor='w', pady=6)
        self.button('Approve this plan and create the system', self.apply_foundation, True)
        self.button('View full plan details', lambda: self.plan_details(record['summary']))
        self.button('Back to configuration', self.scaffold_ready)

    def plan_details(self, value):
        window = tk.Toplevel(self); window.title('Nomiarch plan details'); window.geometry('850x650')
        text = tk.Text(window, wrap='word', padx=16, pady=16)
        scroll = ttk.Scrollbar(window, command=text.yview); text.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y'); text.pack(fill='both', expand=True)
        text.insert('1.0', json.dumps(value, indent=2)); text.configure(state='disabled')

    def apply_foundation(self):
        result = self.pending_foundation
        if not messagebox.askokcancel('Approve deployment', 'Create the system exactly as shown in this plan? Azure resources will incur charges if you selected Azure.'): return
        try: check = self.repository_check()
        except Exception as e: messagebox.showerror('Repository approval', str(e)); return
        directory, expected = result['run_directory'], result['plan']['digest']
        self.start(lambda: deployment.apply(directory, self.repo, expected, repository_check=check), self.installed)

    def resume_foundation(self):
        path = filedialog.askdirectory(title='Select your customer configuration folder')
        if not path: return
        try:
            value, _ = scaffold.load_supported(path, self.repo)
            self.project_path, self.target = Path(path), value['configuration']['target']
            self.isolation.set(value['isolation']); self.mode.set('offline' if value['isolation'] == 'disconnected' else 'online')
        except Exception as e: messagebox.showerror('Open configuration', str(e)); return
        self.resume_after_files = True
        self.download_screen(False)

    def foundation_management(self):
        index = self.listbox.current()
        if index < 0: return
        directory, run = self.records[index]
        if not run.get('foundation'):
            messagebox.showinfo('Customer configuration', 'This installation predates customer scaffolds. Its existing maintenance actions remain available.'); return
        self.managed_foundation = directory
        self.project_path = Path(run['foundation']['project_directory'])
        self.clear('Observe and propose changes', 'Core evaluates measurements against your approved configuration. Proposed changes still need a human review and a separate deployment approval.')
        self.button('Check this environment with Core', self.observe_foundation, True)
        self.button('Propose the observed repair', self.propose_reconciliation)
        self.button('Review a proposed configuration folder', self.review_reconciliation)
        row = ttk.Frame(self.content); row.pack(fill='x', pady=8)
        ttk.Label(row, text='GitHub proposal token', width=24).pack(side='left')
        ttk.Entry(row, textvariable=self.github_token, show='•').pack(side='left', fill='x', expand=True)
        self.watch_enabled = tk.BooleanVar(value=False)
        ttk.Checkbutton(self.content, text='While this app is open, check every minute and open repair PRs',
                        variable=self.watch_enabled, command=self.toggle_foundation_watch).pack(anchor='w', pady=10)
        ttk.Label(self.content, text='The proposal account should only write proposal branches and PRs. Keep repository administration and deployment access with separate human-controlled accounts. Watching stops when you leave this screen.', wraplength=820).pack(anchor='w', pady=10)
        self.button('Back', self.stop_foundation_watch)

    def stop_foundation_watch(self):
        self.watch_enabled.set(False)
        self.manage()

    def observe_foundation(self):
        directory = Path(self.managed_foundation)
        token = self.github_token.get()
        repository_head = GitHub(token).main_head if token else None
        def task():
            run = read_json(directory / 'run.json')
            if run['config']['target'] == 'local': service.locate_multipass()
            else: cloud.tools_ready(self.root_dir, self.repo, self.download_progress)
            return observe.inspect(directory, self.repo, repository_head=repository_head)
        def ready(report):
            self.latest_observation = report
            count = sum(f['status'] == 'fail' for f in report['core']['result']['findings'])
            self.status.set(f'Core found {count} setting(s) that differ from the approved configuration.' if count else 'The measured capacity, release and outbound policy match your configuration.' + (' Repository checked.' if report['repository_checked'] else ' Repository was not checked.'))
            self.events.put(('log', report['core']['result']['explanation']['summary']))
            failed = {f['check'] for f in report['core']['result']['findings'] if f['status'] == 'fail'}
            if count and failed <= {'cpus', 'memory_gib', 'disk_gib', 'outbound_blocked'} and self.watch_enabled.get() and not (directory / 'pending-proposal.json').exists():
                self.propose_reconciliation(automatic=True)
        self.start(task, ready)

    def toggle_foundation_watch(self):
        if not self.watch_enabled.get(): return
        value, _ = scaffold.load_supported(self.project_path, self.repo)
        if value['repository']['mode'] != 'github' or not self.github_token.get():
            self.watch_enabled.set(False)
            messagebox.showinfo('Repository needed', 'Automatic PR proposals require the configured customer GitHub repository and a proposal token. You can still run local checks manually.'); return
        self.observe_foundation()
        def tick():
            if not self.watch_enabled.get(): return
            if not self.busy: self.observe_foundation()
            self.after(60000, tick)
        self.after(60000, tick)

    def propose_reconciliation(self, automatic=False):
        if not hasattr(self, 'latest_observation'):
            messagebox.showinfo('Observe first', 'Run Check this environment with Core before proposing a repair.'); return
        try:
            report = self.latest_observation
            value = observe.proposed_project(self.managed_foundation, self.repo, report)
        except Exception as e: messagebox.showerror('Observe first', str(e)); return
        if not automatic and not messagebox.askokcancel('Propose a repair', 'Save this repair request and open a customer pull request if configured? No settings will be applied.'): return
        token, directory = self.github_token.get(), Path(self.managed_foundation)
        folder = self.root_dir / 'projects' / ('proposal-' + value['reconciliation']['observation_sha256'][:16])
        def task():
            if not folder.exists(): scaffold.create(folder, value, self.repo)
            existing, _ = scaffold.load_supported(folder, self.repo)
            if existing != value: raise NomiarchError('A different proposal already uses this folder')
            result = {'folder': str(folder)}
            if value['repository']['mode'] == 'github':
                result.update(GitHub(token).propose(value, scaffold.render(value, self.repo), 'Restore approved Nomiarch settings',
                    'Core observed a difference in: ' + ', '.join(value['reconciliation']['checks']) + '.\n\nReview the evidence references and reconciliation request. This PR changes no approval policy and performs no deployment. After merge, prepare and approve a new plan in Nomiarch Setup.'))
            write_json(directory / 'pending-proposal.json', result)
            return result
        def ready(result):
            self.status.set('Repair proposed. Human approval and a fresh deployment plan are required.')
            if result.get('url'): webbrowser.open(result['url'])
            else: service.open_folder(result['folder'])
        self.start(task, ready)

    def review_reconciliation(self):
        pending = Path(self.managed_foundation) / 'pending-proposal.json'
        saved = read_json(pending) if pending.exists() else {}
        if saved.get('operation'):
            self.operation_screen(saved)
            return
        folder = saved.get('folder') or filedialog.askdirectory(title='Select the proposed customer configuration')
        if not folder: return
        self.watch_enabled.set(False)
        self.project_path = Path(folder)
        if saved.get('number'): self.pull_number.set(str(saved['number']))
        self.clear('Review the repair request', 'After the customer PR is approved and merged, prepare a new plan. Nothing changes until you approve that plan.')
        self.entry('Merged PR number', self.pull_number)
        row = ttk.Frame(self.content); row.pack(fill='x', pady=8)
        ttk.Label(row, text='GitHub approval-check token', width=24).pack(side='left')
        ttk.Entry(row, textvariable=self.github_token, show='•').pack(side='left', fill='x', expand=True)
        self.button('Open proposed configuration', lambda: service.open_folder(folder))
        def prepare():
            try: check = self.repository_check()
            except Exception as e: messagebox.showerror('Review needed', str(e)); return
            def task():
                value, snapshot = scaffold.load_supported(folder, self.repo)
                if check: check(value, snapshot)
                return changes.prepare(self.managed_foundation, folder, self.repo)
            self.start(task, self.show_change_plan)
        self.button('Prepare repair plan', prepare, True)
        self.button('Back', self.manage)

    def foundation_operation(self, directory, record, action):
        self.managed_foundation = Path(directory)
        paths = {'operation': action}
        if action in {'upgrade', 'repair'}:
            archive = filedialog.askopenfilename(title='Select the admitted Core release bundle', initialdir=self.kit.get(), filetypes=[('Nomiarch release', '*.tar')])
            if not archive: return
            key = filedialog.askopenfilename(title='Select the approved release public key', initialdir=str(Path(archive).parent), filetypes=[('Release public key', '*.pem')])
            if not key: return
            paths.update(archive=archive, key=key)
        if action == 'upgrade':
            keypath = filedialog.asksaveasfilename(title='Save a NEW private recovery key separately from backups', initialfile='nomiarch-recovery-key.txt', defaultextension='.txt')
            if not keypath: return
            try: paths['recipient'] = service.recovery_key(keypath)
            except Exception as e: messagebox.showerror('Recovery key', str(e)); return
            messagebox.showinfo('Keep this recovery key', 'The key stays with you. It is not sent to the VM. Keep it separate from the encrypted backup.')
        def task():
            value = maintenance.propose(directory, self.repo, action, paths.get('archive'), paths.get('key'))
            folder = self.root_dir / 'projects' / (action + '-' + value['maintenance']['id'])
            scaffold.create(folder, value, self.repo)
            pending = dict(paths, folder=str(folder))
            write_json(Path(directory) / 'pending-proposal.json', pending)
            return pending
        self.start(task, self.operation_screen)

    def operation_screen(self, pending):
        self.project_path = Path(pending['folder'])
        if hasattr(self, 'watch_enabled'): self.watch_enabled.set(False)
        if pending.get('number'): self.pull_number.set(str(pending['number']))
        value, _ = scaffold.load_supported(self.project_path, self.repo)
        action = pending['operation']
        self.clear('Review this ' + action + ' request',
                   'This operation has its own request and approval. Preparing its plan does not change the system. Removal permanently deletes the selected VM and disks.' if action == 'destroy' else
                   'Review the selected release and operation before planning. An upgrade makes an encrypted backup before replacing the release.')
        self.button('Open the operation configuration', lambda: service.open_folder(pending['folder']))
        if value['repository']['mode'] == 'github':
            row = ttk.Frame(self.content); row.pack(fill='x', pady=8)
            ttk.Label(row, text='GitHub token', width=24).pack(side='left')
            ttk.Entry(row, textvariable=self.github_token, show='•').pack(side='left', fill='x', expand=True)
            def publish():
                token = self.github_token.get()
                if not messagebox.askokcancel('Open operation PR', 'Open this specific operation request in the customer repository for human review?'): return
                def task():
                    pr = GitHub(token).propose(value, scaffold.render(value, self.repo), action.title() + ' Nomiarch installation',
                        'Review this operation-specific request and the runtime pins. The earlier installation approval does not authorize this action. No infrastructure is changed by this PR; a separate exact-plan approval is required after merge.')
                    saved = dict(pending, **pr)
                    write_json(Path(self.managed_foundation) / 'pending-proposal.json', saved)
                    return saved
                def ready(saved):
                    pending.update(saved); self.pull_number.set(str(saved['number'])); webbrowser.open(saved['url'])
                    self.status.set('Have a designated human review and merge the operation PR, then prepare its plan.')
                self.start(task, ready)
            self.button('Open an operation pull request', publish)
            self.entry('Approved, merged PR number', self.pull_number)
        def prepare():
            try: check = self.repository_check()
            except Exception as e: messagebox.showerror('Repository approval', str(e)); return
            def task():
                value, snapshot = scaffold.load_supported(pending['folder'], self.repo)
                if check: check(value, snapshot)
                run = read_json(Path(self.managed_foundation) / 'run.json')
                if run['config']['target'] == 'local': service.locate_multipass()
                else: cloud.tools_ready(self.root_dir, self.repo, self.download_progress)
                return maintenance.prepare(self.managed_foundation, pending['folder'], self.repo, pending.get('archive'), pending.get('key'), pending.get('recipient'))
            self.start(task, self.show_operation_plan)
        self.button('Prepare ' + action + ' plan', prepare, True)
        self.button('Back', self.manage)

    def show_operation_plan(self, result):
        record = result['plan']; summary = record['summary']; action = summary['action']
        self.clear('Approve this ' + action, 'Approval applies only to this request and the exact release files shown in the configuration. The plan expires after one hour.')
        labels = [('System', summary['system']), ('VM', summary['vm']), ('Destination', summary['target']),
                  ('Current Core', summary['from_release']), ('Selected Core', summary['to_release'])]
        for label, value in labels: ttk.Label(self.content, text=label + ': ' + value, wraplength=820).pack(anchor='w', pady=5)
        note = 'This permanently deletes the VM and its disks. Keep any needed backup first.' if action == 'destroy' else 'Expect downtime. An encrypted backup is required before an upgrade.'
        ttk.Label(self.content, text=note, wraplength=820).pack(anchor='w', pady=8)
        self.button('View plan details', lambda: self.plan_details(summary))
        def apply():
            try: check = self.repository_check()
            except Exception as e: messagebox.showerror('Repository approval', str(e)); return
            if not messagebox.askokcancel('Approve ' + action, note + '\n\nApply this exact operation plan?'): return
            def ready(value):
                (Path(self.managed_foundation) / 'pending-proposal.json').unlink(missing_ok=True)
                self.status.set(action.title() + ' completed and recorded.'); self.manage()
            self.start(lambda: maintenance.apply(result['operation_directory'], self.repo, record['digest'], repository_check=check), ready)
        self.button('Approve and ' + action, apply, True)
        self.button('Back', self.manage)

    def show_change_plan(self, result):
        self.pending_change = result
        record = result['plan']
        self.clear('Approve this repair plan', 'This restores the reviewed configuration. Core cannot approve or apply the repair itself.')
        ttk.Label(self.content, text='A VM restart is required.' if record['downtime'] else 'No VM restart is planned.', wraplength=820).pack(anchor='w', pady=8)
        for action in record['actions']:
            ttk.Label(self.content, text=json.dumps(action), wraplength=820).pack(anchor='w', pady=4)
        ttk.Label(self.content, text='Plan reference: ' + record['digest'][:16]).pack(anchor='w', pady=8)
        def apply():
            try: check = self.repository_check()
            except Exception as e: messagebox.showerror('Review needed', str(e)); return
            if not messagebox.askokcancel('Approve repair', 'Apply exactly this reviewed repair plan?'): return
            def ready(value):
                (Path(self.managed_foundation) / 'pending-proposal.json').unlink(missing_ok=True)
                self.status.set('Repair applied and verified against the approved configuration.')
                self.manage()
            self.start(lambda: changes.apply(result['change_directory'], self.repo, record['digest'], repository_check=check), ready)
        self.button('Approve and apply this repair', apply, True)
        self.button('Back', self.manage)
