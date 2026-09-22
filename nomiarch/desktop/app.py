"""Native desktop wizard. Background work never accesses Tk objects."""
import contextlib
import io
import json
import os
from pathlib import Path
import queue
import re
import sys
import threading
from types import SimpleNamespace
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from nomiarch.bootstrap import bundle, controller
from nomiarch.bootstrap.config import validate
from nomiarch.common import NomiarchError, read_json, write_json
from . import service
from .foundation import FoundationScreens
from .catalog import CORE_VERSION, DESKTOP_VERSION, PINS

BG, PANEL, TEXT, MUTED, ACCENT = '#101a19', '#1a2825', '#eff8f3', '#a6beb4', '#a5edc7'
FONT = 'Segoe UI' if os.name == 'nt' else 'Helvetica Neue' if sys.platform == 'darwin' else 'DejaVu Sans'


class LogSink(io.TextIOBase):
    def __init__(self, events): self.events = events
    def write(self, text):
        if text.strip(): self.events.put(('log', text.strip()))
        return len(text)
    def flush(self): pass


class App(FoundationScreens, tk.Tk):
    def __init__(self, root_dir=None):
        super().__init__()
        self.title('Nomiarch Setup')
        self.geometry('980x760')
        self.minsize(780, 650)
        self.configure(bg=BG)
        self.root_dir = Path(root_dir) if root_dir else service.data_root()
        self.state_dir = self.root_dir / 'state'
        self.repo = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[2]))
        self.events, self.cancel = queue.Queue(), threading.Event()
        self.busy, self.stage, self.can_cancel = False, 0, False
        self.paths = None
        self.name = tk.StringVar(value='nomiarch-local')
        self.mode = tk.StringVar(value='online')
        self.kit = tk.StringVar(value=str(self.root_dir / 'downloads'))
        self.memory = tk.StringVar(value='8')
        self.cpus = tk.StringVar(value='2')
        self.disk = tk.StringVar(value='40')
        self.recipient = tk.StringVar()
        self.foundation_variables()
        self.status = tk.StringVar(value='Welcome. Let’s create your first Nomiarch system.')
        style = ttk.Style(self)
        style.theme_use('clam')
        style.configure('TFrame', background=BG)
        style.configure('TLabel', background=BG, foreground=TEXT, font=(FONT, 11))
        style.configure('TButton', font=(FONT, 11), padding=(15,10))
        style.configure('Accent.TButton', background=ACCENT, foreground=BG)
        style.configure('TRadiobutton', background=BG, foreground=TEXT, font=(FONT,11))
        style.configure('TProgressbar', background=ACCENT, troughcolor=PANEL)
        header = tk.Frame(self, bg=BG)
        header.pack(fill='x', padx=32, pady=(24,12))
        tk.Label(header, text='NOMIARCH', bg=BG, fg=ACCENT, font=(FONT,22,'bold')).pack(side='left')
        tk.Label(header, text=f'Setup {DESKTOP_VERSION}  ·  Core {CORE_VERSION}', bg=BG, fg=MUTED).pack(side='right')
        self.steps = tk.Label(self, bg=BG, fg=ACCENT, font=(FONT,10))
        self.steps.pack(anchor='w', padx=32, pady=8)
        viewport = ttk.Frame(self)
        viewport.pack(fill='both', expand=True, padx=32)
        self.canvas = tk.Canvas(viewport, bg=BG, highlightthickness=0)
        scroll = ttk.Scrollbar(viewport, orient='vertical', command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y'); self.canvas.pack(side='left', fill='both', expand=True)
        self.content = ttk.Frame(self.canvas)
        window = self.canvas.create_window((0,0), window=self.content, anchor='nw')
        self.content.bind('<Configure>', lambda event: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>', lambda event: self.canvas.itemconfigure(window, width=event.width))
        def wheel(event):
            if self.winfo_containing(event.x_root,event.y_root) == self.log: return
            self.canvas.yview_scroll(-1 if event.num==4 else 1 if event.num==5 else (-1 if event.delta>0 else 1), 'units')
        self.bind_all('<MouseWheel>', wheel); self.bind_all('<Button-4>', wheel); self.bind_all('<Button-5>', wheel)
        footer = ttk.Frame(self)
        footer.pack(fill='x', padx=32, pady=16)
        ttk.Label(footer, textvariable=self.status, wraplength=860).pack(anchor='w', pady=(0,10))
        self.progress = ttk.Progressbar(footer, mode='determinate')
        self.progress.pack(fill='x')
        self.log = tk.Text(footer, height=4, bg=PANEL, fg=MUTED, relief='flat', wrap='word', state='disabled')
        self.log.pack(fill='x', pady=(10,0))
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.after(100, self.poll)
        self.home()

    def clear(self, title, intro, stage=''):
        for child in self.content.winfo_children(): child.destroy()
        self.canvas.yview_moveto(0)
        self.steps.configure(text=stage)
        ttk.Label(self.content, text=title, font=(FONT,25,'bold')).pack(anchor='w', pady=(14,12))
        ttk.Label(self.content, text=intro, wraplength=850).pack(anchor='w', pady=(0,20))

    def button(self, text, command, primary=False):
        b = ttk.Button(self.content, text=text, command=command, style='Accent.TButton' if primary else 'TButton')
        b.pack(anchor='w', pady=6)
        return b

    def entry(self, label, variable):
        row = ttk.Frame(self.content); row.pack(fill='x', pady=6)
        ttk.Label(row,text=label,width=24).pack(side='left')
        ttk.Entry(row,textvariable=variable,width=52).pack(side='left',fill='x',expand=True)

    def home(self):
        self.clear('Your AI. Your environment.',
                   'Choose where your system runs. The wizard creates your configuration, shows the plan and asks for your approval before installation.')
        self.button('Set up this computer', lambda: self.customer('local'), True)
        self.button('Set up in the cloud — Azure', lambda: self.customer('azure'))
        self.button('Download an offline kit', lambda: self.download_screen(True))
        self.button('Open a customer configuration', self.resume_foundation)
        self.button('Manage an installation', self.manage)
        ttk.Label(self.content,text='Preview: sample configuration checks, local inference and audit evidence.\nPhysical network isolation remains part of your site setup. Windows Home and ARM Windows are not supported yet.',wraplength=830,foreground=MUTED).pack(anchor='w',pady=20)

    def prerequisites(self):
        self.preflight_ready = False
        self.clear('Check your computer',
                   'Nomiarch runs inside its own Ubuntu VM. We’ll check VM support before downloading the system. Allow at least 8 GB RAM for the VM and 50 GB free disk for setup.', '01  Computer    /    02  Files    /    03  Review    /    04  Install')
        self.button('Check again', self.check, True)
        self.button('Install VM support', self.support)
        if os.name == 'nt': self.button('Enable Hyper-V', self.hyperv)
        self.continue_button = self.button('Continue', lambda: self.download_screen(False))
        self.continue_button.state(['disabled'])
        self.button('Back', self.home)
        self.check()

    def check(self):
        def ready(value):
            self.preflight_ready = True
            self.status.set(value)
            self.continue_button.state(['!disabled'])
        self.start(service.preflight, ready)

    def support(self):
        online, folder = self.isolation.get() != 'disconnected', self.kit.get()
        if not online:
            selected=filedialog.askdirectory(title='Select the admitted kit containing the VM support installer')
            if not selected:return
            folder=selected;self.kit.set(folder)
        if messagebox.askokcancel('Install VM support', 'Open the verified Canonical Multipass installer? Windows/macOS may ask for administrator approval. Your existing VMs will not be removed.'):
            self.start(lambda: service.install_vm_support(folder,self.download_progress,online,self.cancel), lambda value:self.status.set(value), cancellable=True)

    def hyperv(self):
        if messagebox.askokcancel('Enable Hyper-V', 'Windows will request administrator approval to enable Hyper-V. A restart may be required. Continue?'):
            try:
                service.enable_hyperv()
                self.status.set('Complete Windows setup and restart if asked, then reopen Nomiarch.')
            except Exception as e: messagebox.showerror('Windows setup',str(e))

    def download_screen(self, kit_only=False):
        self.clear('Get the system files', 'Downloads are checked automatically against the trusted catalog included in this app. Completed verified files are reused when you retry.', '01  Computer    /    02  Files    /    03  Review    /    04  Install')
        self.kit_only = kit_only
        if not kit_only:
            if self.isolation.get() != 'disconnected':
                ttk.Radiobutton(self.content,text='Download and verify automatically',variable=self.mode,value='online').pack(anchor='w',pady=7)
            ttk.Radiobutton(self.content,text='Use files I brought into this environment (no downloads)',variable=self.mode,value='offline').pack(anchor='w',pady=7)
        else:
            self.mode.set('online')
            ttk.Label(self.content,text='This downloads a kit for the same CPU architecture as this computer, including VM support for Windows/macOS. Bring this app and the kit through your approved transfer process.',wraplength=800).pack(anchor='w',pady=10)
        self.entry('Kit folder',self.kit)
        self.button('Choose folder…',self.choose_kit)
        self.button('Verify files' if self.mode.get()=='offline' else 'Prepare files', self.prepare, True)
        self.button('Cancel download',self.cancel_download)
        self.button('Back',self.home)

    def choose_kit(self):
        directory=filedialog.askdirectory(initialdir=str(self.root_dir))
        if directory:self.kit.set(directory)

    def download_progress(self,name,received,total):
        self.events.put(('download',(name,received,total)))

    def prepare(self):
        folder, online = self.kit.get(), self.mode.get()=='online'
        try: arch='amd64' if self.target=='azure' and not self.kit_only else service.architecture()
        except Exception as e: messagebox.showerror('This computer',str(e));return
        def task():
            paths=service.prepare_kit(folder,arch,online,self.download_progress,self.cancel)
            manifest=bundle.inspect(paths['bundle'],paths['key'])
            if manifest['architecture']!=arch:raise NomiarchError('Bundle architecture mismatch')
            return paths
        def ready(paths):
            self.paths=paths
            if self.kit_only:
                self.status.set('Offline kit verified. Copy this folder and the desktop app through your approved transfer process.')
                messagebox.showinfo('Kit ready','Your offline kit is ready. The isolated computer also needs VM support installed.')
            elif getattr(self,'resume_after_files',False):
                self.resume_after_files=False
                self.scaffold_ready()
            else:self.review()
        self.start(task,ready,cancellable=True)

    def review(self):
        self.clear('Choose your system size', 'The defaults fit the included small model. Next we will save a customer-owned configuration for you to review.', '04  System configuration')
        for label,var in [('System name',self.name),('CPUs',self.cpus),('Memory (GB)',self.memory),('Disk (GB)',self.disk)]:self.entry(label,var)
        ttk.Label(self.content,text=f'Core {CORE_VERSION}  ·  Local model  ·  Destination: {self.target}\nYour configuration is saved before any resources are created.',wraplength=800,foreground=MUTED).pack(anchor='w',pady=16)
        self.button('Create my configuration',self.make_scaffold,True)
        self.button('Back',lambda:self.download_screen(False))

    def installed(self,result):
        if result['validation']['status']!='passed':
            raise NomiarchError('Installation did not complete. Keep the run record and use Manage → Repair or Remove. '+result['validation'].get('error',''))
        self.clear('Your system is ready', 'Core and the local model passed their first task. Choose Verify to run it again, or manage the installation from this app.', '04  Installation verified')
        self.status.set('Installation passed. VM kept for further testing.')
        self.button('Manage my installation',self.manage,True)
        self.button('Home',self.home)

    def manage(self):
        self.clear('Your installations','Select the exact system to verify, repair, upgrade or back up. Saved status is not a live health check.')
        self.records=[]
        self.selection=tk.StringVar()
        for path in sorted(self.state_dir.glob('runs/*/run.json')):
            try:
                record=read_json(path)
                if (record.get('inventory') or record.get('creation_started')) and record.get('cleanup',{}).get('status')!='deleted':self.records.append((path.parent,record))
            except NomiarchError:pass
        options=[f"{r['config']['name']}  ·  {r['phase']}  ·  {p.name[:8]}" for p,r in self.records]
        self.listbox=ttk.Combobox(self.content,values=options,textvariable=self.selection,state='readonly',width=75)
        self.listbox.pack(anchor='w',pady=8)
        if options:self.listbox.current(0)
        self.button('Import an existing run folder…',self.import_run)
        row=ttk.Frame(self.content);row.pack(fill='x',pady=10)
        for label,action in [('Verify','verify'),('Repair','repair'),('Upgrade','upgrade'),('Back up','backup'),('Saved status','status')]:
            ttk.Button(row,text=label,command=lambda a=action:self.maintenance(a)).pack(side='left',padx=(0,6))
        self.button('Remove selected VM…',lambda:self.maintenance('destroy'))
        self.button('Observe and review foundation changes',self.foundation_management)
        self.button('Home',self.home)

    def import_run(self):
        folder=filedialog.askdirectory(title='Select the folder containing run.json')
        if folder:
            try:
                record=controller.dispatch(SimpleNamespace(action='status',run=folder,repo=self.repo))
                self.records.append((Path(folder),record))
                values=list(self.listbox['values'])+[f"{record['config']['name']}  ·  imported  ·  {Path(folder).name[:8]}"]
                self.listbox['values']=values;self.listbox.current(len(values)-1)
            except Exception as e:messagebox.showerror('Import failed',str(e))

    def maintenance(self,action):
        i=self.listbox.current()
        if i<0:messagebox.showinfo('Select an installation','Select or import an installation first.');return
        directory,record=self.records[i]
        if record.get('foundation') and action in ('repair','upgrade','destroy'):
            self.foundation_operation(directory,record,action)
            return
        args=SimpleNamespace(action=action,run=str(directory),repo=self.repo)
        if action in ('repair','upgrade'):
            args.bundle=filedialog.askopenfilename(title='Select signed Core bundle',filetypes=[('Nomiarch bundle','*.tar')])
            if not args.bundle:return
            args.trusted_key=filedialog.askopenfilename(title='Select approved release public key',filetypes=[('Public key','*.pem')])
            if not args.trusted_key:return
        if action in ('backup','upgrade'):
            keypath=filedialog.asksaveasfilename(title='Save a NEW private recovery key separately from your backups',defaultextension='.txt',initialfile='nomiarch-recovery-key.txt')
            if not keypath:return
            try:args.backup_recipient=service.recovery_key(keypath)
            except Exception as e:messagebox.showerror('Recovery key',str(e));return
            messagebox.showinfo('Keep your recovery key','Store this key securely. You will need it to restore the encrypted backup. It is never sent to the VM.')
        warning=''
        if action=='destroy':warning='This permanently deletes the selected VM and its disks. Export a backup first. '
        if action=='upgrade':warning='This creates an encrypted backup and replaces the release. Expect downtime. '
        if not messagebox.askokcancel(action.title(),warning+f"Run {action} on {record['config']['name']} ({directory.name[:8]})?"):return
        def task():
            service.locate_multipass() if record['config']['target']=='local' else None
            return controller.dispatch(args)
        def done(result):
            if result.get('validation',{}).get('status')=='failed' or result.get('cleanup',{}).get('status')=='failed' or result.get('backup_export_error'):
                raise NomiarchError(json.dumps(result,indent=2))
            self.status.set(action.title()+' completed. The result is recorded in the installation folder.')
            self.events.put(('log',json.dumps(result,indent=2)))
            if action=='destroy':self.manage()
        self.start(task,done)

    def set_buttons(self,disabled):
        def walk(widget):
            for child in widget.winfo_children():
                if isinstance(child,(ttk.Button,ttk.Entry,ttk.Combobox,ttk.Radiobutton,ttk.Checkbutton)):
                    if not (isinstance(child,ttk.Button) and child.cget('text')=='Cancel download'):
                        child.state(['disabled'] if disabled else ['!disabled'])
                walk(child)
        walk(self.content)
        if not disabled and hasattr(self,'continue_button') and self.continue_button.winfo_exists() and not self.preflight_ready:
            self.continue_button.state(['disabled'])

    def start(self,task,done,cancellable=False):
        if self.busy:return
        self.busy,self.can_cancel=True,cancellable
        self.cancel.clear();self.set_buttons(True)
        self.progress.configure(mode='indeterminate');self.progress.start(12)
        self.status.set('Working… please keep Nomiarch open.')
        def worker():
            try:
                with contextlib.redirect_stdout(LogSink(self.events)):
                    result=task()
                self.events.put(('done',(done,result)))
            except Exception as e:self.events.put(('error',str(e)))
        threading.Thread(target=worker,daemon=True).start()

    def cancel_download(self):
        if self.busy and self.can_cancel:self.cancel.set();self.status.set('Cancelling download…')

    def poll(self):
        try:
            while True:
                kind,value=self.events.get_nowait()
                if kind=='log':
                    self.log.configure(state='normal');self.log.insert('end',value+'\n');self.log.see('end');self.log.configure(state='disabled')
                    if value.startswith('Run directory:'):self.status.set('Creating and installing your VM. This can take several minutes.')
                elif kind=='download':
                    name,received,total=value
                    self.progress.stop();self.progress.configure(mode='determinate',value=100*received/total if total else 0)
                    self.status.set(f'{name}: {received/(1024**2):.0f} MB'+(f' of {total/(1024**2):.0f} MB' if total else ''))
                elif kind=='device-code':
                    self.status.set(value)
                    self.log.configure(state='normal');self.log.insert('end',value+'\n');self.log.see('end');self.log.configure(state='disabled')
                    import webbrowser
                    webbrowser.open('https://microsoft.com/devicelogin')
                else:
                    self.busy=False;self.progress.stop();self.set_buttons(False)
                    if kind=='error':self.status.set('Stopped. Your saved installation records are retained.');messagebox.showerror('Nomiarch needs your attention',value)
                    else:
                        done,result=value
                        try:done(result)
                        except Exception as e:messagebox.showerror('Nomiarch needs your attention',str(e))
        except queue.Empty:pass
        self.after(100,self.poll)

    def close(self):
        if self.busy:
            messagebox.showinfo('Operation in progress','Keep this window open while installation or maintenance runs. Downloads can be cancelled using Cancel download.');return
        self.destroy()


def main():
    if '--self-test' in sys.argv:
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            from PIL import ImageGrab
            app=App(folder)
            output=Path(sys.argv[sys.argv.index('--self-test')+1])
            def internal_repository():
                from nomiarch.common import canonical
                from unittest.mock import patch
                app.repo_mode.set('github-enterprise'); app.usage.set('organisation'); app.isolation.set('disconnected')
                app.server_url.set('https://git.company.internal'); app.repo_owner.set('customer'); app.approvers.set('reviewer')
                app.github_screen()
                app.github_token.set('temporary-test-token')
                with patch.object(app, 'prerequisites') as next_step:
                    app.repository_continue(); next_step.assert_called_once()
                settings = app.repository_settings()
                assert app.token_scope == canonical(settings)
                assert app.repository_token(settings) == 'temporary-test-token'
                app.server_url.set('https://another.internal')
                assert app.github_token.get() == ''
                app.server_url.set('https://git.company.internal')
                app.github_token.set('temporary-test-token')
                app.clear('Resume internal configuration', 'The token is scoped to the selected server and repository.')
                app.repository_token_entry(dict(settings, name='another-project'))
                assert app.github_token.get() == ''
                app.github_screen()
            screens=[('home',app.home),('customer',lambda:app.customer('local')),('repository',app.repository_screen),
                     ('github',app.github_screen),('internal-repository',internal_repository),('azure',app.cloud_screen),('files',app.download_screen),('capacity',app.review),('manage',app.manage)]
            for name,screen in screens:
                screen();app.update()
                app.set_buttons(True);app.set_buttons(False);app.update()
                ImageGrab.grab().save(output.with_name(output.stem+'-'+name+'.png'))
            app.home();app.update();ImageGrab.grab().save(output.with_suffix('.png'))
            if getattr(sys,'frozen',False):
                import subprocess
                from .cloud import packaged_helper
                helper=packaged_helper(app.repo)
                if not helper:raise NomiarchError('Packaged Microsoft helper is missing')
                env=dict(os.environ,AZURE_CONFIG_DIR=str(Path(folder)/'azure-test'),AZURE_CORE_COLLECT_TELEMETRY='no')
                def helper_output(args):
                    result=subprocess.run([helper,*args],env=env,timeout=120,capture_output=True,text=True,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                    if result.returncode or 'ERROR:' in result.stderr:
                        raise NomiarchError('Packaged Microsoft helper failed: '+result.stderr[-2000:])
                    return result.stdout
                version=json.loads(helper_output(['version']))
                if version.get('azure-cli')!='2.90.0':raise NomiarchError('Microsoft helper version differs')
                for args in (['login','--help'],['account','--help'],['vm','image','list','--help'],['network','nsg','show','--help'],['group','delete','--help']):
                    helper_output(args)
            app.destroy()
            output.write_text('Desktop screens passed')
        return
    App().mainloop()


if __name__=='__main__':main()
