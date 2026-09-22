"""Persistent guest egress boundary; the controller/physical host remains trusted."""
import json


# Dedicated chains coexist with K3s. Only replies, loopback, DHCP and the two
# fixed K3s ranges are permitted. No general RFC1918 or DNS exception is made.
SCRIPT = r'''#!/usr/bin/python3
import json, shutil, socket, subprocess, sys
def command(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout
def install():
    for tool in ('iptables', 'ip6tables'):
        if not shutil.which(tool): raise RuntimeError(tool + ' is required')
        for parent in ('OUTPUT', 'FORWARD'):
            chain = 'NOMIARCH_' + parent
            subprocess.run([tool, '-w', '-N', chain], capture_output=True)
            command(tool, '-w', '-F', chain)
            command(tool, '-w', '-A', chain, '-m', 'conntrack', '--ctstate', 'ESTABLISHED,RELATED', '--ctdir', 'REPLY', '-j', 'RETURN')
            if parent == 'OUTPUT':
                command(tool, '-w', '-A', chain, '-o', 'lo', '-j', 'RETURN')
                if tool == 'iptables':
                    command(tool, '-w', '-A', chain, '-p', 'udp', '--sport', '68', '--dport', '67', '-j', 'RETURN')
            if tool == 'iptables':
                for network in ('10.42.0.0/16', '10.43.0.0/16'):
                    command(tool, '-w', '-A', chain, '-d', network, '-j', 'RETURN')
            command(tool, '-w', '-A', chain, '-j', 'REJECT')
            while subprocess.run([tool, '-w', '-C', parent, '-j', chain], capture_output=True).returncode == 0:
                command(tool, '-w', '-D', parent, '-j', chain)
            command(tool, '-w', '-I', parent, '1', '-j', chain)
def verify():
    for tool in ('iptables', 'ip6tables'):
        for parent in ('OUTPUT', 'FORWARD'):
            chain = 'NOMIARCH_' + parent
            rules = command(tool, '-w', '-S', parent).splitlines()
            first = next((line for line in rules if line.startswith('-A ')), '')
            if first != '-A ' + parent + ' -j ' + chain:
                raise RuntimeError('Egress boundary is not first in ' + tool + ' ' + parent)
            lines = command(tool, '-w', '-S', chain).splitlines()
            expected = [['-m', 'conntrack', '--ctstate', 'ESTABLISHED,RELATED', '--ctdir', 'REPLY', '-j', 'RETURN']]
            if parent == 'OUTPUT':
                expected.append(['-o', 'lo', '-j', 'RETURN'])
                if tool == 'iptables': expected.append(['-p', 'udp', '--sport', '68', '--dport', '67', '-j', 'RETURN'])
            if tool == 'iptables':
                expected += [['-d', n, '-j', 'RETURN'] for n in ('10.42.0.0/16', '10.43.0.0/16')]
            expected.append(['-j', 'REJECT'])
            if len([line for line in lines if line.startswith('-A ')]) != len(expected):
                raise RuntimeError('Unexpected egress exception')
            for rule in expected: command(tool, '-w', '-C', chain, *rule)
            if not lines[-1].startswith('-A ' + chain + ' -j REJECT'):
                raise RuntimeError('Missing final rejection')
    # Direct probes bypass DNS and proxy configuration. A routing failure alone
    # is not proof; both persistent rules and these probes must pass.
    for family, host in ((socket.AF_INET, '1.1.1.1'), (socket.AF_INET6, '2606:4700:4700::1111')):
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.settimeout(3)
            if sock.connect_ex((host, 443)) == 0:
                raise RuntimeError('Unexpected external network access')
    command('systemctl', 'is-enabled', 'nomiarch-egress.service')
    return {'guest_egress': 'blocked', 'ipv4_ipv6_rules': 'present', 'direct_https_probes': 'blocked',
            'persistence': 'enabled; validate with a site reboot test', 'physical_air_gap': 'not established by this check'}
if __name__ == '__main__':
    if sys.argv[1] == 'install': install()
    else: print(json.dumps(verify()))
'''

UNIT = '''[Unit]
Description=Nomiarch guest egress boundary
After=network-pre.target
Before=network.target k3s.service
Wants=network-pre.target
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/nomiarch-egress install
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
'''


def configure(provider):
    # Fixed source shipped with the controller; customer strings never enter it.
    writer = "from pathlib import Path; import json,sys; v=json.load(sys.stdin); " \
             "p=Path('/usr/local/sbin/nomiarch-egress'); p.write_text(v['script']); p.chmod(0o700); " \
             "Path('/etc/systemd/system/nomiarch-egress.service').write_text(v['unit'])"
    provider.remote(['sudo', '-n', 'python3', '-c', writer], input=json.dumps({'script': SCRIPT, 'unit': UNIT}).encode())
    provider.remote(['sudo', '-n', 'systemctl', 'daemon-reload'])
    provider.remote(['sudo', '-n', 'systemctl', 'enable', '--now', 'nomiarch-egress.service'])
    return verify(provider)


def verify(provider):
    return json.loads(provider.remote(['sudo', '-n', '/usr/local/sbin/nomiarch-egress', 'verify'], timeout=30))
