"""Infrastructure-only bootstrap screens (milestone 1). Tk values are read on the UI
thread; background work receives plain values and never touches widgets."""
import json
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from nomiarch.common import NomiarchError
from nomiarch.foundation import backup, deployment, infrastructure, scaffold
from . import service, setup_flow
from .catalog import infrastructure_inventory


class InfrastructureScreens:
    def infrastructure_variables(self):
        for name, value in {'location': 'local', 'provider': 'multipass', 'git_service': 'github', 'network_mode': 'restricted-connected',
                            'host_architecture': 'amd64', 'existing_host': '', 'existing_user': 'nomiarch',
                            'existing_key': '', 'existing_known_hosts': ''}.items():
            setattr(self, name, tk.StringVar(value=value))
        self.flow = 'core'
        self.infrastructure_paths = {}

    # --- 1. Destination -------------------------------------------------------------
    def infrastructure_home(self):
        self.flow = 'infrastructure'
        self.project_path, self.paths = None, None
        self.usage.set('organisation')
        self.clear('Establish your infrastructure first',
                   'This path creates or adopts the infrastructure that will later run Nomi. It needs no model, '
                   'model hardware or Core release. Nothing is created before you review a plan and approve it explicitly.',
                   '01  Destination    /    02  Git service    /    03  Network mode    /    04  Settings    /    05  Review')
        for label, var in [('Organisation or project', self.organisation), ('Environment', self.environment)]: self.entry(label, var)
        ttk.Label(self.content, text='Where should the infrastructure live?', font=('', 12, 'bold')).pack(anchor='w', pady=(12, 4))
        for name, label in setup_flow.location_options():
            ttk.Radiobutton(self.content, text=label, variable=self.location, value=name, command=self.location_changed).pack(anchor='w', pady=6)
        self.provider_frame = ttk.Frame(self.content); self.provider_frame.pack(fill='x', pady=4)
        self.location_changed()
        self.button('Continue', self.location_continue, True)
        self.button('Back', self.home)

    def location_changed(self):
        for child in self.provider_frame.winfo_children(): child.destroy()
        location = self.location.get()
        options = setup_flow.provider_options(location)
        self.provider.set(options[0][0])
        if location == 'cloud':
            ttk.Label(self.provider_frame, text='Cloud provider', width=24).pack(side='left')
            box = ttk.Combobox(self.provider_frame, state='readonly', values=[label for _, label, _ in options], width=40)
            box.current(0); box.pack(side='left')
            box.bind('<<ComboboxSelected>>', lambda event: self.provider.set(options[box.current()][0]))
            ttk.Label(self.provider_frame, text='Azure is the only implemented provider. Other clouds and on-premises hypervisors are added as separate adapters and are not shown as working options.', wraplength=800).pack(anchor='w', pady=6)
        for _, _, note in options:
            ttk.Label(self.provider_frame, text=note, wraplength=800).pack(anchor='w', pady=4)
        if location == 'on-premises':
            ttk.Label(self.provider_frame, text='On-premises adoption does not create the VM. Your organisation provides a dedicated Ubuntu 24.04 host and trusted SSH access.', wraplength=800, foreground='#a5edc7').pack(anchor='w', pady=4)

    def location_continue(self):
        try:
            scaffold.slug(self.organisation.get(), 'Organisation'); scaffold.slug(self.environment.get(), 'Environment')
            setup_flow.provider_options(self.location.get())
        except Exception as error: messagebox.showerror('Check the names', str(error)); return
        self.target = scaffold.destinations.target_for(self.location.get(), self.provider.get())
        self.git_service_screen()

    # --- 2. Git service ---------------------------------------------------------------
    def git_service_screen(self):
        self.github_token.set('')
        self.clear('Choose your Git service', 'Both services use the same review and approval model: a protected main branch, the trusted validation check, '
                   'an independent human reviewer and a separate deployment approval.', '02  Git service')
        for name, label, note in setup_flow.git_service_options():
            ttk.Radiobutton(self.content, text=label, variable=self.git_service, value=name, command=self.git_service_changed).pack(anchor='w', pady=6)
            if note: ttk.Label(self.content, text=note, wraplength=800).pack(anchor='w', padx=28)
        self.git_frame = ttk.Frame(self.content); self.git_frame.pack(fill='x', pady=8)
        self.git_service_changed()
        self.button('Continue', self.git_service_continue, True)
        self.button('Back', self.infrastructure_home)

    def git_service_changed(self):
        for child in self.git_frame.winfo_children(): child.destroy()
        self.repo_mode.set(self.git_service.get())
        internal = self.git_service.get() == 'github-enterprise'
        def row(label, var, show=None):
            r = ttk.Frame(self.git_frame); r.pack(fill='x', pady=6)
            ttk.Label(r, text=label, width=24).pack(side='left')
            ttk.Entry(r, textvariable=var, width=52, **({'show': show} if show else {})).pack(side='left', fill='x', expand=True)
        if internal:
            row('Internal HTTPS address', self.server_url)
            ttk.Label(self.git_frame, text='Your existing GitHub Enterprise Server, for example https://git.company.internal. It must resolve to private addresses. Selecting it does not install or license a Git server.', wraplength=800).pack(anchor='w', pady=4)
            ttk.Button(self.git_frame, text='Choose public CA certificate…', command=self.choose_repository_ca).pack(anchor='w', pady=4)
            ttk.Button(self.git_frame, text='Use this computer’s certificate trust', command=self.reset_repository_ca).pack(anchor='w', pady=4)
            ttk.Label(self.git_frame, textvariable=self.ca_status, wraplength=800).pack(anchor='w', pady=4)
        row('Git owner', self.repo_owner); row('Repository name', self.repo_name); row('Independent reviewer logins', self.approvers)
        row('Bootstrap admin token (not saved)', self.github_token, '•')
        ttk.Label(self.git_frame, text='The token stays in this app’s memory and is used once to create and protect the repository. It is separate from future Nomi identities, which never receive it. Reviewers must be different people from the person running setup.', wraplength=800).pack(anchor='w', pady=6)

    def git_service_continue(self):
        try:
            self.token_scope = None
            settings = setup_flow.repository_settings(self.git_service.get(), self.repo_owner.get(), self.repo_name.get(), self.approvers.get(),
                                                      self.server_url.get(), self.repository_ca)
            from nomiarch.common import canonical
            self.token_scope = canonical(settings)
        except Exception as error: messagebox.showerror('Repository settings', str(error)); return
        self.network_mode_screen()

    # --- 3. Network mode --------------------------------------------------------------
    def network_mode_screen(self):
        self.clear('Choose the network mode', 'Restricted connectivity is never substituted for an air gap. Incompatible combinations are refused here, before anything is generated.', '03  Network mode')
        for name, label in setup_flow.network_mode_options():
            ttk.Radiobutton(self.content, text=label, variable=self.network_mode, value=name).pack(anchor='w', pady=6)
        ttk.Label(self.content, text='Fully disconnected requires Local or On-premises infrastructure and GitHub Enterprise Server inside the boundary. A private IP or firewall does not prove a physical air gap; the site qualifies that separately.', wraplength=800).pack(anchor='w', pady=8)
        self.button('Continue', self.network_mode_continue, True)
        self.button('Back', self.git_service_screen)

    def network_mode_continue(self):
        problems = setup_flow.problems(self.location.get(), self.provider.get(), self.git_service.get(), self.network_mode.get())
        if problems:
            messagebox.showerror('Incompatible choices', '\n\n'.join(problems) + '\n\nChange a choice; the network mode is not downgraded for you.'); return
        self.isolation.set(scaffold.destinations.isolation_for(self.target, self.network_mode.get()))
        self.mode.set('offline' if self.network_mode.get() == 'fully-disconnected' else 'online')
        self.infrastructure_settings_screen()

    # --- 4. Settings ------------------------------------------------------------------
    def infrastructure_settings_screen(self):
        groups = setup_flow.settings_needed(self.location.get(), self.provider.get())
        self.clear('Infrastructure settings', 'Only the settings for your selected destination are shown. Capacity is yours to choose; no model requirement applies yet.', '04  Settings')
        for item in setup_flow.prerequisites(self.location.get(), self.provider.get(), self.git_service.get(), self.network_mode.get()):
            ttk.Label(self.content, text='• ' + item, wraplength=820).pack(anchor='w', pady=2)
        ttk.Label(self.content, text=' ').pack()
        self.entry('System name', self.name)
        if 'architecture' in groups:
            row = ttk.Frame(self.content); row.pack(fill='x', pady=6)
            ttk.Label(row, text='CPU architecture', width=24).pack(side='left')
            ttk.Combobox(row, textvariable=self.host_architecture, state='readonly', values=['amd64', 'arm64'], width=12).pack(side='left')
        else:
            arch = 'amd64' if self.target == 'azure' else service.architecture()
            ttk.Label(self.content, text='CPU architecture: ' + arch + (' (Azure reference recipe)' if self.target == 'azure' else ' (this computer)')).pack(anchor='w', pady=4)
        for label, var in [('vCPUs', self.cpus), ('Memory (GB)', self.memory), ('Storage (GB)', self.disk)]: self.entry(label, var)
        if 'ssh-host' in groups:
            self.entry('Host name or private IP', self.existing_host)
            self.entry('SSH administration user', self.existing_user)
            self.button('Choose SSH private key…', lambda: self.pick_file(self.existing_key, 'Choose the SSH private key for the adopted host'))
            ttk.Label(self.content, textvariable=self.existing_key, wraplength=820).pack(anchor='w')
            self.button('Choose known_hosts with the enrolled host key…', lambda: self.pick_file(self.existing_known_hosts, 'Choose the known_hosts file enrolled out of band'))
            ttk.Label(self.content, textvariable=self.existing_known_hosts, wraplength=820).pack(anchor='w')
            ttk.Label(self.content, text='The private key and known_hosts are copied into the controller’s private run directory only. They never enter the repository.', wraplength=820).pack(anchor='w', pady=6)
        if 'admitted-image' in groups:
            ttk.Label(self.content, text='Next, the admitted Ubuntu 24.04 image is verified from your kit folder (downloaded when connected; never downloaded in fully disconnected mode).', wraplength=820).pack(anchor='w', pady=6)
        if 'azure-sign-in' in groups:
            ttk.Label(self.content, text='Next, sign in to Microsoft Azure, choose the subscription, privately routed subnet, management range and VM size.', wraplength=820).pack(anchor='w', pady=6)
        self.button('Continue', self.infrastructure_settings_continue, True)
        self.button('Back', self.network_mode_screen)

    def pick_file(self, variable, title):
        path = filedialog.askopenfilename(title=title)
        if path: variable.set(path)

    def infrastructure_settings_continue(self):
        try:
            self.infrastructure_capacity = setup_flow.capacity(self.cpus.get(), self.memory.get(), self.disk.get())
            if self.target == 'existing':
                self.existing_settings = setup_flow.existing_host_settings(self.existing_host.get(), self.existing_user.get(), self.existing_key.get(), self.existing_known_hosts.get())
            local_architecture = service.architecture() if self.target == 'local' else None
            self.infrastructure_architecture = setup_flow.architecture_for(self.location.get(), self.provider.get(), local_architecture, self.host_architecture.get())
        except Exception as error: messagebox.showerror('Check the settings', str(error)); return
        if self.target == 'local': self.download_screen(False)
        elif self.target == 'azure': self.cloud_screen()
        else: self.infrastructure_review()

    # --- 5. Review and create -----------------------------------------------------------
    def infrastructure_settings(self):
        settings = {'name': self.name.get(), 'architecture': self.infrastructure_architecture, 'capacity': self.infrastructure_capacity}
        if self.target == 'local':
            settings['image'] = (self.paths or {}).get('image')
            if not settings['image']: raise NomiarchError('Verify the admitted Ubuntu image first')
        elif self.target == 'existing':
            settings['existing'] = self.existing_settings
        else:
            settings['azure'] = {'subscription_id': self.azure_subscription, 'location': self.region.get(), 'vm_size': self.vm_size.get(),
                                 'ssh_user': 'nomiarch', 'admin_cidr': self.admin_cidr.get().strip(), 'subnet_id': self.azure_subnet, 'image': self.azure_image}
        return settings

    def infrastructure_choices(self):
        return {'location': self.location.get(), 'provider': self.provider.get(), 'git_service': self.git_service.get(),
                'network_mode': self.network_mode.get(), 'organisation': self.organisation.get(), 'environment': self.environment.get()}

    def infrastructure_review(self):
        try:
            repository = setup_flow.repository_settings(self.git_service.get(), self.repo_owner.get(), self.repo_name.get(), self.approvers.get(),
                                                        self.server_url.get(), self.repository_ca, 'organisation', self.isolation.get())
            value = setup_flow.build(self.infrastructure_choices(), self.infrastructure_settings(), repository)
        except Exception as error: messagebox.showerror('Check the settings', str(error)); return
        self.pending_value = value
        if not self.project_folder.get() or Path(self.project_folder.get()).exists():
            self.project_folder.set(str(self.root_dir / 'projects' / (self.organisation.get() + '-' + self.environment.get() + '-infrastructure')))
        self.clear('Review before creating the configuration', 'Creating the configuration writes files only. The repository, pull request, plan and deployment are separate explicit steps.', '05  Review')
        described = scaffold.destinations.describe(value['destination']['location'], value['destination']['provider'], self.git_service.get(), self.network_mode.get())
        capacity = value['configuration']['capacity']
        for label, text in [('Destination', described['location'] + ' · ' + described['provider'] + (' (creates a VM)' if described['creates_vm'] else ' (adopts an existing host; no VM is created)')),
                            ('Git service', scaffold.destinations.GIT_SERVICES[self.git_service.get()]['label']),
                            ('Network mode', described['network_mode'] + ' · ' + described['physical_air_gap']),
                            ('Capacity', f"{capacity['cpus']} vCPU · {capacity['memory_gib']} GB memory · {capacity['disk_gib']} GB storage · {value['configuration']['architecture']}"),
                            ('Reviewers', ', '.join(value['repository']['approvers'])),
                            ('Scope', 'infrastructure only — no Core, model, agent or container is installed')]:
            ttk.Label(self.content, text=label + ': ' + text, wraplength=840).pack(anchor='w', pady=4)
        self.entry('New configuration folder', self.project_folder)
        self.button('Choose parent folder…', self.choose_project_parent)
        self.button('Create my configuration', self.make_infrastructure_scaffold, True)
        self.button('View kit inventory', lambda: self.plan_details(infrastructure_inventory(value['configuration']['architecture'])))
        self.button('Back', self.infrastructure_settings_screen)

    def make_infrastructure_scaffold(self):
        value, folder = self.pending_value, self.project_folder.get()
        try: self.infrastructure_paths = setup_flow.controller_paths(value, self.infrastructure_settings())
        except Exception as error: messagebox.showerror('Check the settings', str(error)); return
        def ready(path):
            self.project_path = path
            self.scaffold_ready()
        self.start(lambda: scaffold.create(folder, value, self.repo), ready)

    # --- Status, readiness and recovery --------------------------------------------------
    def infrastructure_runs(self, value):
        return infrastructure.existing_runs(self.state_dir, value['id'])

    def infrastructure_status_block(self, runs, value=None):
        for run in runs:
            ttk.Label(self.content, text=f"Existing run {Path(run['run_directory']).name[:8]} · phase: {run['phase']} · infrastructure: "
                      f"{(run.get('validation') or {}).get('status', 'pending')} · application: {(run.get('application') or {}).get('status', 'n/a')}",
                      wraplength=840, foreground='#a5edc7').pack(anchor='w', pady=4)
        pending = deployment.saved_plan(self.state_dir, value['id'], self.repo) if value is not None and runs else None
        if pending and pending['status'] == 'valid':
            ttk.Label(self.content, text='A saved plan is waiting for your approval. Reopen it to review exactly what will be created; approving it still requires the current repository review check.', wraplength=840).pack(anchor='w', pady=4)
            self.button('Review the saved plan', lambda: self.show_foundation_plan(pending), True)
        elif pending and pending['unused']:
            ttk.Label(self.content, text=f"The saved plan is {pending['status']} and was never used: {pending['reason']} "
                      'Retire it to keep its record and permanently prevent it from running, then prepare a replacement plan.', wraplength=840).pack(anchor='w', pady=4)
            self.button('Retire this unused plan', lambda: self.retire_saved_plan(pending), True)
        elif runs:
            ttk.Label(self.content, text='This configuration already has a retained run. Planning again is refused so no duplicate resources are created. Use Manage to inspect, verify, back up or prepare a reviewed removal.', wraplength=840).pack(anchor='w', pady=4)
            self.button('Open Manage', self.manage)

    def retire_saved_plan(self, pending):
        if not messagebox.askokcancel('Retire unused plan', 'Retire plan ' + pending['plan']['digest'][:16] + '? It will never run. Its records are kept, '
                                      'and a replacement plan needs fresh validation, the current repository review and your explicit approval.'):
            return
        directory = pending['run_directory']
        reason = 'Operator replaced an unused ' + pending['status'] + ' plan from setup'
        def ready(evidence):
            self.status.set('Plan retired and recorded. Prepare a fresh plan for review.')
            self.scaffold_ready()
        self.start(lambda: deployment.retire_plan(directory, self.repo, reason), ready)

    def infrastructure_ready(self, result):
        status = infrastructure.status(result['run_directory'])
        if result['validation']['status'] != 'passed':
            self.clear('Infrastructure provisioning did not complete', 'Logs, the plan and any partial state are retained for recovery. Nothing was deleted.', '06  Needs attention')
            ttk.Label(self.content, text='Error: ' + result['validation'].get('error', ''), wraplength=840).pack(anchor='w', pady=6)
            if status.get('recovery'): ttk.Label(self.content, text=status['recovery']['instruction'], wraplength=840).pack(anchor='w', pady=6)
            ttk.Label(self.content, text='Run directory: ' + result['run_directory'], wraplength=840).pack(anchor='w', pady=6)
            self.button('Open Manage', self.manage, True); self.button('Home', self.home)
            return
        self.clear('Infrastructure ready', 'The infrastructure is verified and recorded. No application, model or agent is installed; that is a separate later operation with its own review.', '06  Infrastructure ready')
        checks = result['validation'].get('checks', {})
        lines = [('Phase', status['phase']), ('Inventory', json.dumps(result.get('inventory'))),
                 ('Capacity', json.dumps(checks.get('observed', {}))), ('Network', json.dumps(checks.get('network', {}))),
                 ('Application', (result.get('application') or {}).get('status', 'not-installed')),
                 ('Repository commit', str(status.get('repository') or 'local records')),
                 ('Executor', 'this human-operated controller; no automated pipeline ran')]
        for label, text in lines: ttk.Label(self.content, text=label + ': ' + text, wraplength=840).pack(anchor='w', pady=3)
        self.status.set('Infrastructure ready. Application installation is a later milestone.')
        self.button('Back up this foundation…', lambda: self.backup_foundation(result['run_directory']), True)
        self.button('View verification details', lambda: self.plan_details(status))
        self.button('Manage', self.manage); self.button('Home', self.home)

    def backup_foundation(self, directory):
        output = filedialog.asksaveasfilename(title='Save the foundation backup (repository export, records, inventory, trust)', initialfile='nomiarch-foundation-backup.zip', defaultextension='.zip')
        if not output: return
        def ready(result):
            manifest = result['manifest']
            messagebox.showinfo('Backup saved', 'Saved ' + result['file'] + '\n\nKept separately (back up with your key management): ' + ', '.join(manifest['kept_separately'])
                                + ('\n\nExported only in an encrypted backup: ' + ', '.join(manifest['exported_only_when_encrypted']) if manifest['exported_only_when_encrypted'] else ''))
        self.start(lambda: backup.export(directory, output, self.repo), ready)

    def infrastructure_status_screen(self, directory):
        try: status = infrastructure.status(directory)
        except Exception as error: messagebox.showerror('Status', str(error)); return
        self.clear('Infrastructure status', 'Last recorded controller state for this foundation; not a live health query.')
        for label, key in [('Phase', 'phase'), ('Destination', 'destination'), ('Inventory', 'inventory'), ('Infrastructure validation', 'validation'),
                           ('Application', 'application'), ('Approval', 'approval'), ('Repository commit', 'repository'), ('Retained logs', 'retained_logs'), ('Recovery', 'recovery')]:
            ttk.Label(self.content, text=label + ': ' + json.dumps(status.get(key)), wraplength=840).pack(anchor='w', pady=3)
        self.button('Back up this foundation…', lambda: self.backup_foundation(directory), True)
        self.button('Back', self.manage)
