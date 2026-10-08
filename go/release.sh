#!/bin/sh
# Сборка готовых программ gasem для Windows, Linux и macOS → dist/.
#
#   go/release.sh
#
# Каждый архив: программа gasem, справочник языка (gasem.txt), примеры и
# GasemOS — исходники и готовый образ диска gasemos.img.
set -eu
cd "$(dirname "$0")"
root=$(cd .. && pwd)
version=$(sed -n 's/^const Version = "\(.*\)"/\1/p' gasem/version.go)
out="$root/dist"
rm -rf "$out"
mkdir -p "$out"
image="$out/gasemos.img"
go run ./cmd/gasem build -q "$root/os/gasemos.gsm" -o "$image"

for target in windows/amd64 windows/arm64 linux/amd64 linux/arm64 darwin/amd64 darwin/arm64; do
    os=${target%/*}
    arch=${target#*/}
    name="gasem-$version-$os-$arch"
    dir="$out/$name"
    mkdir -p "$dir/examples"
    exe=gasem
    [ "$os" = windows ] && exe=gasem.exe
    CGO_ENABLED=0 GOOS=$os GOARCH=$arch go build -trimpath -ldflags "-s -w" -o "$dir/$exe" ./cmd/gasem
    cp "$root/docs/gasem.txt" "$dir/"
    cp "$root"/examples/*.gsm "$dir/examples/"
    mkdir -p "$dir/gasemos/apps" "$dir/gasemos/screenshots"
    cp "$root"/os/*.gsm "$root/os/README.md" "$root/os/gasemfs.py" "$dir/gasemos/"
    cp "$root"/os/apps/*.gsm "$dir/gasemos/apps/"
    cp "$root"/os/screenshots/*.png "$dir/gasemos/screenshots/"
    cp "$image" "$dir/gasemos/"
    (
        cd "$out"
        if [ "$os" = windows ]; then
            zip -qr "$name.zip" "$name"
        else
            tar -czf "$name.tar.gz" "$name"
        fi
    )
    rm -rf "$dir"
    echo "dist/$(ls "$out" | grep "^$name\.")"
done
rm "$image"

# расширение VS Code (подсветка, сниппеты) — если есть Node.js
if command -v npx >/dev/null 2>&1; then
    (cd "$root/editors/vscode" && npx --yes @vscode/vsce package --skip-license -o "$out/gasem-$version.vsix" >/dev/null)
    echo "dist/gasem-$version.vsix"
fi
