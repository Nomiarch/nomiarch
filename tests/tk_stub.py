"""Headless stand-in for tkinter so desktop screen logic can run in CI without a display.

It records widgets, variables and dialogs. It is not a rendering test; native
package CI renders the real screens with --self-test.
"""
import sys
import types


class Var:
    def __init__(self, value=None, **kwargs):
        self._value, self._traces = value, []
    def get(self): return self._value
    def set(self, value):
        self._value = value
        for callback in list(self._traces): callback()
    def trace_add(self, mode, callback): self._traces.append(callback)


class Widget:
    registry = []
    def __init__(self, master=None, **kwargs):
        self.master, self.options, self.children, self._state = master, dict(kwargs), [], set()
        if master is not None and hasattr(master, 'children'): master.children.append(self)
        Widget.registry.append(self)
    def pack(self, **kwargs): return self
    def pack_forget(self): return self
    def winfo_children(self): return list(self.children)
    def winfo_exists(self): return True
    def winfo_containing(self, *args): return None
    def destroy(self):
        if self.master is not None and hasattr(self.master, 'children') and self in self.master.children: self.master.children.remove(self)
    def state(self, flags=None):
        if flags is None: return tuple(self._state)
        for flag in flags:
            (self._state.discard if flag.startswith('!') else self._state.add)(flag.lstrip('!'))
    def cget(self, name): return self.options.get(name)
    def configure(self, **kwargs): self.options.update(kwargs)
    config = configure
    def bind(self, *args, **kwargs): pass
    def bind_all(self, *args, **kwargs): pass
    def current(self, index=None):
        if index is not None: self._current = index
        return getattr(self, '_current', -1 if not self.options.get('values') else 0)
    def insert(self, *args): pass
    def set(self, *args): pass
    def get(self, *args): return ''
    def see(self, *args): pass
    def yview(self, *args): pass
    def yview_moveto(self, *args): pass
    def yview_scroll(self, *args): pass
    def create_window(self, *args, **kwargs): return 1
    def bbox(self, *args): return (0, 0, 1, 1)
    def itemconfigure(self, *args, **kwargs): pass
    def start(self, *args): pass
    def stop(self, *args): pass
    def title(self, *args): pass
    def geometry(self, *args): pass
    def minsize(self, *args): pass
    def protocol(self, *args): pass
    def after(self, *args): pass
    def update(self): pass
    def __getitem__(self, name): return self.options.get(name)
    def __setitem__(self, name, value): self.options[name] = value
    def invoke(self):
        command = self.options.get('command')
        return command() if command else None


class Tk(Widget):
    def __init__(self, *args, **kwargs):
        super().__init__(None)
        self.children = []


class Style:
    def __init__(self, *args): pass
    def theme_use(self, *args): pass
    def configure(self, *args, **kwargs): pass


class Dialogs:
    def __init__(self):
        self.errors, self.infos, self.answers, self.files = [], [], [], []
        self.ok = True
    def showerror(self, title, message): self.errors.append((title, message))
    def showinfo(self, title, message): self.infos.append((title, message))
    def askokcancel(self, title, message): self.answers.append((title, message)); return self.ok
    def askdirectory(self, **kwargs): return self.files.pop(0) if self.files else ''
    def askopenfilename(self, **kwargs): return self.files.pop(0) if self.files else ''
    def asksaveasfilename(self, **kwargs): return self.files.pop(0) if self.files else ''


def install():
    """Insert the stub modules and return the dialog recorder. Call remove() afterwards."""
    dialogs = Dialogs()
    tk = types.ModuleType('tkinter')
    for name in ('Frame', 'Label', 'Canvas', 'Text', 'Toplevel', 'Entry', 'Button', 'Scrollbar'):
        setattr(tk, name, type(name, (Widget,), {}))
    tk.Tk, tk.StringVar, tk.BooleanVar, tk.IntVar = Tk, Var, Var, Var
    ttk = types.ModuleType('tkinter.ttk')
    for name in ('Frame', 'Label', 'Button', 'Entry', 'Radiobutton', 'Combobox', 'Checkbutton', 'Scrollbar', 'Progressbar'):
        setattr(ttk, name, type(name, (Widget,), {}))
    ttk.Style = Style
    filedialog, messagebox = types.ModuleType('tkinter.filedialog'), types.ModuleType('tkinter.messagebox')
    for name in ('askdirectory', 'askopenfilename', 'asksaveasfilename'): setattr(filedialog, name, getattr(dialogs, name))
    for name in ('showerror', 'showinfo', 'askokcancel'): setattr(messagebox, name, getattr(dialogs, name))
    tk.ttk, tk.filedialog, tk.messagebox = ttk, filedialog, messagebox
    saved = {name: sys.modules.get(name) for name in ('tkinter', 'tkinter.ttk', 'tkinter.filedialog', 'tkinter.messagebox',
                                                      'nomiarch.desktop.app', 'nomiarch.desktop.foundation', 'nomiarch.desktop.infrastructure')}
    sys.modules.update({'tkinter': tk, 'tkinter.ttk': ttk, 'tkinter.filedialog': filedialog, 'tkinter.messagebox': messagebox})
    package = sys.modules.get('nomiarch.desktop')
    for name in ('nomiarch.desktop.app', 'nomiarch.desktop.foundation', 'nomiarch.desktop.infrastructure'):
        sys.modules.pop(name, None)
        # `from nomiarch.desktop import app` reuses a package attribute; drop it so the
        # screens are re-imported against the stub every time.
        if package is not None and hasattr(package, name.rsplit('.', 1)[1]): delattr(package, name.rsplit('.', 1)[1])
    dialogs.saved = saved
    return dialogs


def remove(dialogs):
    package = sys.modules.get('nomiarch.desktop')
    for name, module in dialogs.saved.items():
        if module is None: sys.modules.pop(name, None)
        else: sys.modules[name] = module
        if name.startswith('nomiarch.desktop.') and package is not None:
            attribute = name.rsplit('.', 1)[1]
            if module is None:
                if hasattr(package, attribute): delattr(package, attribute)
            else: setattr(package, attribute, module)
