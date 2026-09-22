"""Reviewed download pins. Desktop 0.1.0.dev3 installs the tested Core dev2."""
DESKTOP_VERSION = '0.1.0.dev3'
CORE_VERSION = '0.1.0.dev2'
BASE = 'https://github.com/Nomiarch/nomiarch/releases/download/v0.1.0.dev2/'
IMAGE_BASE = 'https://cloud-images.ubuntu.com/releases/noble/release-20260911/'
PINS = {
 'amd64': {
  'bundle': ('nomiarch-amd64.tar', '4ef4bb78ceb4f6506353c44ffe92f79795e650fda38455e5796bf1c440319acc'),
  'key': ('nomiarch-amd64-public.pem', '86b8ab1ffea27613daaa60547ead24cdd00acf40f3962b383fa1286bcc4f6e62'),
  'image': ('ubuntu-24.04-server-cloudimg-amd64.img', '612b2c0cc1bc413a6cb8c38fd611794caf0f2b436c50013d8b3794db12ad7354')},
 'arm64': {
  'bundle': ('nomiarch-arm64.tar', 'ed822a48750154c320623d91303c25de355ef52d18795f03437be5e27ce18230'),
  'key': ('nomiarch-arm64-public.pem', '02b9f0d0b6f4c760ebdc20e43d6d23c78eeb8b771ad375e4bbfb21a3c3768496'),
  'image': ('ubuntu-24.04-server-cloudimg-arm64.img', '7b682958a67ff5de068e36de6af8b75fa645d296af5a70d6500527f6a33781db')},
}
PREREQUISITES = {
 'Windows': ('https://github.com/canonical/multipass/releases/download/v1.16.4/multipass-1.16.4%2Bwin-win64.msi', 'b0c417fb8254fa61e4aa63895f59354bb3717c58100816651a9300dd98124aea', 'Multipass.msi'),
 'Darwin': ('https://github.com/canonical/multipass/releases/download/v1.16.4/multipass-1.16.4%2Bmac-Darwin.pkg', 'e481704f65bc1650ae8aa5c873eb9137e8b9b403eac60aa603b68d8b4d2c281c', 'Multipass.pkg'),
}
