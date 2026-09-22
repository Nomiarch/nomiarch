"""Reviewed download pins. Desktop 0.1.0.dev4 installs the tested Core dev3."""
DESKTOP_VERSION = '0.1.0.dev4'
RELEASE_READY = True  # Published Core artifacts passed both architecture gates.
CORE_VERSION = '0.1.0.dev3'
BASE = 'https://github.com/Nomiarch/nomiarch/releases/download/v0.1.0.dev3/'
IMAGE_BASE = 'https://cloud-images.ubuntu.com/releases/noble/release-20260911/'
PINS = {
 'amd64': {
  'bundle': ('nomiarch-amd64.tar', '4896aeb3ddb2fcbecd264c2ff798bdb4d63ab7f75e184d2d2217e1f346e96a85'),
  'key': ('nomiarch-amd64-public.pem', '5d93c72f8580f20479db91c14e5bf7612f777e5c3b2708f2a1cbfd51dd8d82d8'),
  'image': ('ubuntu-24.04-server-cloudimg-amd64.img', '612b2c0cc1bc413a6cb8c38fd611794caf0f2b436c50013d8b3794db12ad7354')},
 'arm64': {
  'bundle': ('nomiarch-arm64.tar', 'c48311eac091b4e8cbdcfee2bcb19b9555e3e4cdbae0282e9c6c2a958e8d2c94'),
  'key': ('nomiarch-arm64-public.pem', '63274f1215becddf1cedf59aa88311090f2678f720bba8d5a5f7ce04bbc431db'),
  'image': ('ubuntu-24.04-server-cloudimg-arm64.img', '7b682958a67ff5de068e36de6af8b75fa645d296af5a70d6500527f6a33781db')},
}
PREREQUISITES = {
 'Windows': ('https://github.com/canonical/multipass/releases/download/v1.16.4/multipass-1.16.4%2Bwin-win64.msi', 'b0c417fb8254fa61e4aa63895f59354bb3717c58100816651a9300dd98124aea', 'Multipass.msi'),
 'Darwin': ('https://github.com/canonical/multipass/releases/download/v1.16.4/multipass-1.16.4%2Bmac-Darwin.pkg', 'e481704f65bc1650ae8aa5c873eb9137e8b9b403eac60aa603b68d8b4d2c281c', 'Multipass.pkg'),
}
