"""Linux CI: real packets in two disposable network namespaces.

Run as root. No rules or network links are changed in the runner's namespace.
"""
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nomiarch.foundation.network import SCRIPT


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def connected(family, host):
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        return sock.connect_ex((host, 443)) == 0


def isolated_test():
    peer_code = '''import socket,threading,sys
def serve(s):
    while True:
        c,a=s.accept(); c.close()
for family,host in ((socket.AF_INET,'0.0.0.0'),(socket.AF_INET6,'::')):
    s=socket.socket(family,socket.SOCK_STREAM)
    if family==socket.AF_INET6:s.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_V6ONLY,1)
    s.bind((host,443));s.listen();threading.Thread(target=serve,args=(s,),daemon=True).start()
print('ready',flush=True)
sys.stdin.read()
'''
    peer = subprocess.Popen(['unshare', '--net', sys.executable, '-c', peer_code], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        assert peer.stdout.readline().strip() == b'ready'
        run('ip', 'link', 'set', 'lo', 'up')
        run('ip', 'link', 'add', 'guest0', 'type', 'veth', 'peer', 'name', 'peer0')
        run('ip', 'link', 'set', 'peer0', 'netns', str(peer.pid))
        run('ip', 'addr', 'add', '10.99.0.1/24', 'dev', 'guest0')
        run('ip', '-6', 'addr', 'add', 'fd99::1/64', 'dev', 'guest0', 'nodad')
        run('ip', 'link', 'set', 'guest0', 'up')
        prefix = ['nsenter', '-t', str(peer.pid), '--net', '--']
        for args in (['link', 'set', 'lo', 'up'], ['addr', 'add', '10.99.0.2/24', 'dev', 'peer0'],
                     ['addr', 'add', '1.1.1.1/32', 'dev', 'peer0'], ['addr', 'add', '10.42.0.1/32', 'dev', 'peer0'],
                     ['-6', 'addr', 'add', 'fd99::2/64', 'dev', 'peer0', 'nodad'],
                     ['-6', 'addr', 'add', '2606:4700:4700::1111/128', 'dev', 'peer0', 'nodad'], ['link', 'set', 'peer0', 'up']):
            run(*prefix, 'ip', *args)
        run('ip', 'route', 'add', 'default', 'via', '10.99.0.2')
        run('ip', '-6', 'route', 'add', 'default', 'via', 'fd99::2')
        assert connected(socket.AF_INET, '1.1.1.1'), 'IPv4 test route must work before the boundary'
        assert connected(socket.AF_INET6, '2606:4700:4700::1111'), 'IPv6 test route must work before the boundary'
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            (path / 'boundary.py').write_text(SCRIPT)
            # Only service enrollment is stubbed in this disposable namespace.
            (path / 'systemctl').write_text('#!/bin/sh\nexit 0\n'); (path / 'systemctl').chmod(0o700)
            os.environ['PATH'] = folder + os.pathsep + os.environ['PATH']
            run(sys.executable, str(path / 'boundary.py'), 'install')
            assert not connected(socket.AF_INET, '1.1.1.1'), 'IPv4 escaped the boundary'
            assert not connected(socket.AF_INET6, '2606:4700:4700::1111'), 'IPv6 escaped the boundary'
            assert connected(socket.AF_INET, '10.42.0.1'), 'The admitted K3s range must remain usable'
            run(sys.executable, str(path / 'boundary.py'), 'verify')
            run('iptables', '-I', 'NOMIARCH_OUTPUT', '1', '-j', 'RETURN')
            assert subprocess.run([sys.executable, str(path / 'boundary.py'), 'verify'], capture_output=True).returncode != 0, 'An added exception must fail verification'
            run(sys.executable, str(path / 'boundary.py'), 'install')
            run(sys.executable, str(path / 'boundary.py'), 'verify')
            print('Guest IPv4/IPv6 denial, K3s exception, tamper detection and rule restoration passed')
    finally:
        peer.terminate(); peer.wait(timeout=10)


if __name__ == '__main__':
    if '--isolated' in sys.argv: isolated_test()
    else:
        result = subprocess.run(['unshare', '--net', sys.executable, str(Path(__file__).resolve()), '--isolated'])
        raise SystemExit(result.returncode)
