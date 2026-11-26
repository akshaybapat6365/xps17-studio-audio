# Maintainer: Akshay Bapat <https://github.com/akshaybapat6365>
pkgname=xps17-studio-audio
pkgver=1.1.0
pkgrel=1
pkgdesc="7-stage AVX2/FMA + Rust Studio Speaker Audio DSP for any Omarchy machine + DMI-guarded Dell XPS 17 9720 (0AFF) FIR & rt1316 DKMS"
arch=('x86_64')
url="https://github.com/akshaybapat6365/xps17-studio-audio"
license=('MIT' 'GPL-2.0-only')
depends=('pipewire' 'wireplumber' 'python' 'bash')
makedepends=('cargo' 'gcc' 'binutils')
optdepends=(
  'dkms: build optional Dell XPS 17 9720 rt1316-sdca hardware crossover module'
  'linux-headers: headers for optional rt1316-sdca DKMS module'
)
source=()
sha256sums=()

build() {
  cd "$startdir"
  if [[ -f dsp-engine/Cargo.toml ]]; then
    (cd dsp-engine && cargo build --release)
  fi
}

package() {
  cd "$startdir"
  install -d "$pkgdir/usr/share/$pkgname"
  cp -a README.md ARCHITECTURE.md LICENSE install.sh uninstall.sh bootstrap.sh \
    acoustics-research verify-integrate kernel-driver "$pkgdir/usr/share/$pkgname/"

  install -d "$pkgdir/usr/lib/ladspa" "$pkgdir/usr/bin"
  if [[ -f dsp-engine/target/release/libxps17_studio_dsp.so ]]; then
    install -Dm755 dsp-engine/target/release/libxps17_studio_dsp.so "$pkgdir/usr/lib/ladspa/libxps17_studio_dsp.so"
    install -Dm755 dsp-engine/target/release/libxps17_studio_dsp.so "$pkgdir/usr/share/$pkgname/dsp-engine/lib/libxps17_studio_dsp.so"
  elif [[ -f dsp-engine/lib/libxps17_studio_dsp.so ]]; then
    install -Dm755 dsp-engine/lib/libxps17_studio_dsp.so "$pkgdir/usr/lib/ladspa/libxps17_studio_dsp.so"
    install -Dm755 dsp-engine/lib/libxps17_studio_dsp.so "$pkgdir/usr/share/$pkgname/dsp-engine/lib/libxps17_studio_dsp.so"
  fi

  if [[ -f dsp-engine/target/release/xps17-studio-cli ]]; then
    install -Dm755 dsp-engine/target/release/xps17-studio-cli "$pkgdir/usr/bin/xps17-studio-cli"
    install -Dm755 dsp-engine/target/release/xps17-studio-cli "$pkgdir/usr/share/$pkgname/dsp-engine/bin/xps17-studio-cli"
  elif [[ -f dsp-engine/bin/xps17-studio-cli ]]; then
    install -Dm755 dsp-engine/bin/xps17-studio-cli "$pkgdir/usr/bin/xps17-studio-cli"
    install -Dm755 dsp-engine/bin/xps17-studio-cli "$pkgdir/usr/share/$pkgname/dsp-engine/bin/xps17-studio-cli"
  fi

  install -Dm755 verify-integrate/staged/portable/studio-audio-profile "$pkgdir/usr/bin/studio-audio-profile"
  install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
}
