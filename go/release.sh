#!/bin/sh
# Сборка готовых программ gasem для Windows, Linux и macOS → dist/.
#
#   go/release.sh
#
# Каждый архив: программа gasem, справочник языка (gasem.txt) и примеры.
set -eu
cd "$(dirname "$0")"
root=$(cd .. && pwd)
version=$(sed -n 's/^const Version = "\(.*\)"/\1/p' gasem/version.go)
out="$root/dist"
rm -rf "$out"
mkdir -p "$out"

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
