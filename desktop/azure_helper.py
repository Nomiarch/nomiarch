"""Packaged official Azure CLI entry point, invoked without a terminal window."""
import sys
from azure.cli.core import get_default_cli

if __name__ == '__main__':
    sys.exit(get_default_cli().invoke(sys.argv[1:]))
